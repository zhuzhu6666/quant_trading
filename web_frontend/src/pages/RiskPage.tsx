import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Gauge, ShieldAlert, ShieldCheck } from "lucide-react";
import { MetricCard } from "@/components/Card";
import { CompactMetric as RiskMiniMetric, Field, StatTile, toneFromStatus } from "@/components/DashboardBits";
import { StatusPill } from "@/components/StatusPill";
import {
  getRecentTradeTraces,
  getRiskPolicyVerdicts,
  getRiskSummary,
} from "@/api/client";
import { getSystemDbHealth } from "@/api/domains/system";
import {
  asRecord,
  formatDirection,
  pick,
  pickArray,
  pickBoolean,
  pickNumber,
  pickString,
} from "@/lib/compat";
import { translateDisplayValue, translateReasonText } from "@/lib/display";
import { formatDecimal, formatTime } from "@/lib/format";
import { useBackendReadinessQuery } from "@/hooks/useCoreQueries";
import { factBoundTone, factHasDisplayValue, factIsKnown, readFact, readFactComponent } from "@/api/fact";
import { decodeCanonicalRiskSnapshot, knownMetric } from "@/api/riskSnapshot";

function itemLabel(value: unknown): string {
  const item = asRecord(value);
  return translateDisplayValue(pickString(item, ["name", "component", "id", "key"], typeof value === "string" ? value : ""));
}

function compactId(value: string): string {
  if (!value || value === "") return "";
  if (value.length <= 12) return value;
  return `${value.slice(0, 8)}...${value.slice(-4)}`;
}

function extractTraceToken(summary: string, key: string): string {
  if (!summary || summary === "") return "";
  const match = summary.match(new RegExp(`${key}=([^;]+)`));
  return match ? translateReasonText(match[1].trim()) : "";
}

function traceOutcomeTone(outcome: string): "ok" | "warn" | "bad" | "mute" {
  if (!outcome || outcome === "") return "mute";
  if (outcome.includes("盈利") || outcome.includes("优秀")) return "ok";
  if (outcome.includes("亏损")) return "bad";
  if (outcome.includes("可接受") || outcome.includes("持平")) return "warn";
  return "mute";
}

function pnlTone(value: string): "ok" | "bad" | "mute" {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed === 0) return "mute";
  return parsed > 0 ? "ok" : "bad";
}

function metricValue(value: number | null, digits = 4, suffix = ""): string {
  return value === null ? "未知" : `${formatDecimal(value, digits)}${suffix}`;
}

export function RiskPage() {
  const riskQuery = useQuery({
    queryKey: ["risk-summary"],
    queryFn: getRiskSummary,
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
  const dbQuery = useQuery({
    queryKey: ["db-health", "risk"],
    queryFn: getSystemDbHealth,
    refetchInterval: 15_000,
    staleTime: 5_000,
  });
  const readinessQuery = useBackendReadinessQuery();
  const policyQuery = useQuery({
    queryKey: ["risk-policy-verdicts"],
    queryFn: () => getRiskPolicyVerdicts(50),
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
  const tradeTracesQuery = useQuery({
    queryKey: ["risk-trade-traces"],
    queryFn: () => getRecentTradeTraces(20),
    refetchInterval: 30_000,
    staleTime: 10_000,
  });

  const risk = asRecord(riskQuery.data);
  const canonicalRisk = decodeCanonicalRiskSnapshot(riskQuery.data);
  // The dedicated policy endpoint is the only source for this panel. Falling
  // back to risk.summary would bind one endpoint's data to another endpoint's
  // freshness and could turn an old nested projection green.
  const policy = asRecord(policyQuery.data);
  const systemHealth = asRecord(risk.system_health);
  const readiness = asRecord(readinessQuery.data);
  const readinessDimensions = asRecord(readiness.readiness_dimensions);
  const readinessBlockers = asRecord(readinessDimensions.blockers);
  const readinessRisk = asRecord(readiness.risk_metrics);
  const riskInputsFact = readFactComponent(riskQuery.data, "risk_inputs", "risk.inputs.v1");
  const systemHealthFact = readFactComponent(riskQuery.data, "system_health", "system.runtime-health.v1");
  const dbFact = readFact(dbQuery.data, "system.db-health.v2");
  const readinessFact = readFact(readinessQuery.data, "ops.backend-readiness.v2");
  const policyFact = readFact(policyQuery.data, "risk.policy-verdicts.v2");
  const traceFact = readFact(tradeTracesQuery.data, "risk.trade-trace-recent.v2");
  const riskRequestFailed = riskQuery.isError || riskQuery.isRefetchError;
  const dbRequestFailed = dbQuery.isError || dbQuery.isRefetchError;
  const readinessRequestFailed = readinessQuery.isError || readinessQuery.isRefetchError;
  const policyRequestFailed = policyQuery.isError || policyQuery.isRefetchError;
  const traceRequestFailed = tradeTracesQuery.isError || tradeTracesQuery.isRefetchError;
  const riskKnown = factIsKnown(riskInputsFact, riskRequestFailed) && canonicalRisk.contractKnown;
  const healthKnown = factIsKnown(systemHealthFact, riskRequestFailed);
  const dbKnown = factIsKnown(dbFact, dbRequestFailed);
  const readinessKnown = factIsKnown(readinessFact, readinessRequestFailed);
  const traceKnown = factIsKnown(traceFact, traceRequestFailed);
  const riskDisplayable = factHasDisplayValue(riskInputsFact) && canonicalRisk.contractKnown;
  const healthDisplayable = factHasDisplayValue(systemHealthFact);
  const dbDisplayable = factHasDisplayValue(dbFact);
  const readinessDisplayable = factHasDisplayValue(readinessFact);
  const varKnown = riskKnown && knownMetric(canonicalRisk.var95.status);
  const var99Known = riskKnown && knownMetric(canonicalRisk.var99.status);
  const kellyKnown = riskKnown && knownMetric(canonicalRisk.kelly.status);
  const stressKnown = riskKnown && knownMetric(canonicalRisk.stress.status);
  const concentrationKnown = riskKnown && knownMetric(canonicalRisk.concentration.status);

  const policyCounts = asRecord(policy.counts);
  const allowed = pickNumber(policyCounts, ["allowed"], 0);
  const blocked = pickNumber(policyCounts, ["blocked"], 0);
  const totalVerdicts = allowed + blocked;
  const allowedRate = totalVerdicts ? (allowed / totalVerdicts) * 100 : 0;
  const policyItems = useMemo(() => pickArray(policy, ["items", "recent_items", "decisions", "history"]), [policy]);
  const tradeTraces = useMemo(() => pickArray(tradeTracesQuery.data, ["items", "traces", "rows"]), [tradeTracesQuery.data]);

  const riskHealth = pickString(systemHealth, ["overall"], "");
  const riskBlocked = pickBoolean(systemHealth, ["trading_blocked", "blocked"], false);
  const critical = pickArray(systemHealth, ["critical_components", "blocking_components"]);
  const degraded = pickArray(systemHealth, ["degraded_components"]);
  const impact = pickString(systemHealth, ["impact_summary", "status", "summary"], "");
  const readinessReadyReported = pickBoolean(readiness, ["ready_for_frontend"], false);
  const schema = pickString(readiness, ["schema_version"], "");
  const blockers = pickArray(readinessBlockers, ["live_execution"]);
  const readinessRiskKnown = pickBoolean(readinessRisk, ["ok"], false)
    && pickString(readinessRisk, ["var_status"], "") === "known";

  const dbList = pickArray(dbQuery.data, ["databases", "database_list", "items"]);
  const dbErrorCount = dbList.reduce<number>((acc, item) => {
    const row = asRecord(item);
    const freshness = pickString(row, ["freshness", "status", "state"], "");
    const exists = pickBoolean(row, ["exists"], true);
    const hasIssues = pickArray(row, ["errors", "issues"]).length > 0;
    return acc + (!exists || hasIssues || ["missing", "stale", "old", "error"].includes(freshness) ? 1 : 0);
  }, 0);
  const dbStatus = pickString(dbQuery.data, ["overall", "status"], "");
  const riskBlockers = [...critical, ...blockers];
  const latestPolicy = asRecord(policyItems[0]);
  const latestTrace = asRecord(tradeTraces[0]);
  const latestPolicyAllowed = policyItems.length ? pickBoolean(latestPolicy, ["allowed", "ok", "pass"], false) : undefined;
  const latestPolicyReason = policyItems.length ? translateReasonText(pickString(latestPolicy, ["reason", "message"], "")) : "";
  const latestTraceOutcome = tradeTraces.length ? translateDisplayValue(pickString(latestTrace, ["outcome_label"], "")) : "";
  const hasSystemQueryError = riskRequestFailed || dbRequestFailed || readinessRequestFailed;
  const systemFactsKnown = healthKnown && riskKnown && dbKnown && readinessKnown;

  return (
    <section className="dashboard risk-dashboard">
      <div className="dashboard-header">
        <div>
          <div className="eyebrow">风控审计</div>
          <h1>风控审计</h1>
          <p>展示 canonical 前瞻风险分布、策略裁决和运行阻断，不在浏览器重算风险。</p>
        </div>
        <div className="header-status">
          <StatusPill status={riskDisplayable ? `风险 ${translateDisplayValue(canonicalRisk.status)}` : "风险状态未知"} tone={factBoundTone(riskInputsFact, varKnown ? "ok" : "warn", riskRequestFailed)} />
          <StatusPill status={healthDisplayable ? (riskBlocked ? "交易阻断" : "健康面未阻断") : "交易许可未知"} tone={factBoundTone(systemHealthFact, riskBlocked ? "bad" : "ok", riskRequestFailed)} />
          <StatusPill status={readinessDisplayable ? (readinessReadyReported ? "后端就绪" : "后端受限") : "后端就绪状态未知"} tone={factBoundTone(readinessFact, readinessReadyReported ? "ok" : "warn", readinessRequestFailed)} />
        </div>
      </div>

      <div className="stat-grid">
        <StatTile icon={ShieldCheck} label="系统健康" value={healthDisplayable ? translateDisplayValue(riskHealth) : "未知"} detail={translateDisplayValue(impact)} tone={factBoundTone(systemHealthFact, toneFromStatus(riskHealth), riskRequestFailed)} />
        <StatTile icon={ShieldAlert} label="策略拦截" value={formatDecimal(blocked, 0)} detail={`允许 ${formatDecimal(allowed, 0)} · 通过率 ${formatDecimal(allowedRate, 1)}%`} tone={factBoundTone(policyFact, blocked ? "warn" : "ok", policyRequestFailed)} />
        <StatTile icon={Gauge} label="前瞻 VaR 95%" value={varKnown ? metricValue(canonicalRisk.var95.varPct, 4, "%") : translateDisplayValue(canonicalRisk.var95.status || "unknown")} detail={varKnown ? `CVaR ${metricValue(canonicalRisk.var95.cvarPct, 4, "%")} · ${canonicalRisk.var95.timeframe}` : "等待 canonical 风险输入"} tone={factBoundTone(riskInputsFact, varKnown ? "mute" : "warn", riskRequestFailed)} />
        <StatTile icon={AlertTriangle} label="阻断组件" value={formatDecimal(critical.length + blockers.length, 0)} detail={`退化 ${formatDecimal(degraded.length, 0)} · DB 异常 ${formatDecimal(dbErrorCount, 0)}`} tone={!systemFactsKnown ? "warn" : critical.length || blockers.length ? "bad" : degraded.length || dbErrorCount ? "warn" : "ok"} />
      </div>

      <div className="dashboard-grid">
        <MetricCard title="Canonical 风险快照" className="wide-panel risk-control-overview">
          <div className="risk-mini-grid">
            <RiskMiniMetric label="95% VaR / CVaR" value={varKnown ? `${metricValue(canonicalRisk.var95.varPct, 4, "%")} / ${metricValue(canonicalRisk.var95.cvarPct, 4, "%")}` : translateDisplayValue(canonicalRisk.var95.status || "unknown")} detail={varKnown ? `${metricValue(canonicalRisk.var95.varUsd, 2, " USD")} / ${metricValue(canonicalRisk.var95.cvarUsd, 2, " USD")}` : "未形成可裁决分布"} tone={factBoundTone(riskInputsFact, varKnown ? "mute" : "warn", riskRequestFailed)} />
            <RiskMiniMetric label="99% Shadow" value={var99Known ? `${metricValue(canonicalRisk.var99.varPct, 4, "%")} / ${metricValue(canonicalRisk.var99.cvarPct, 4, "%")}` : translateDisplayValue(canonicalRisk.var99.status || "unknown")} detail="只读双算，不增加阈值" tone={factBoundTone(riskInputsFact, var99Known ? "mute" : "warn", riskRequestFailed)} />
            <RiskMiniMetric label="Kelly" value={kellyKnown ? metricValue(canonicalRisk.kelly.fraction, 4) : translateDisplayValue(canonicalRisk.kelly.status || "unknown")} detail={kellyKnown ? `样本 ${metricValue(canonicalRisk.kelly.closedTrades, 0)} · 胜率 ${metricValue(canonicalRisk.kelly.winRate === null ? null : canonicalRisk.kelly.winRate * 100, 1, "%")}` : "等待已闭合交易样本"} tone={factBoundTone(riskInputsFact, kellyKnown ? "mute" : "warn", riskRequestFailed)} />
            <RiskMiniMetric label="策略通过率" value={`${formatDecimal(allowedRate, 1)}%`} detail={`允许 ${formatDecimal(allowed, 0)} / 拦截 ${formatDecimal(blocked, 0)}`} tone={factBoundTone(policyFact, blocked ? "warn" : "ok", policyRequestFailed)} />
            <RiskMiniMetric label="组件异常" value={formatDecimal(riskBlockers.length + degraded.length, 0)} detail={`阻断 ${formatDecimal(riskBlockers.length, 0)} · 退化 ${formatDecimal(degraded.length, 0)}`} tone={!systemFactsKnown ? "warn" : riskBlockers.length ? "bad" : degraded.length ? "warn" : "ok"} />
            <RiskMiniMetric label="数据健康" value={!dbDisplayable ? "未知" : dbErrorCount ? `${formatDecimal(dbErrorCount, 0)} 异常` : translateDisplayValue(dbStatus)} detail={`库 ${formatDecimal(dbList.length, 0)} · 合约 ${schema}${dbRequestFailed ? " · 接口异常" : ""}`} tone={factBoundTone(dbFact, dbErrorCount ? "bad" : toneFromStatus(dbStatus), dbRequestFailed)} />
          </div>

          <div className="risk-control-grid">
            <section className="risk-control-section">
              <div className="risk-section-head">
                <h3>前瞻分布</h3>
                <StatusPill status={varKnown ? "已冻结" : translateDisplayValue(canonicalRisk.var95.status || "unknown")} tone={factBoundTone(riskInputsFact, varKnown ? "ok" : "warn", riskRequestFailed)} />
              </div>
              <div className="field-list risk-compact-fields">
                <Field label="周期" value={varKnown ? `${canonicalRisk.var95.horizon} · ${canonicalRisk.var95.timeframe}` : "未知"} />
                <Field label="收益样本" value={varKnown ? metricValue(canonicalRisk.var95.sampleCount, 0) : "未知"} />
                <Field label="当前权益" value={varKnown ? metricValue(canonicalRisk.var95.currentEquity, 2, " USD") : "未知"} />
                <Field label="当前净名义敞口" value={varKnown ? metricValue(canonicalRisk.var95.currentNetNotionalUsd, 2, " USD") : "未知"} />
                <Field label="数据窗口" value={varKnown ? `${formatTime(canonicalRisk.sourceWindowStart)} → ${formatTime(canonicalRisk.sourceWindowEnd)}` : "未知"} />
                <Field label="输入指纹" value={canonicalRisk.inputFingerprint ? compactId(canonicalRisk.inputFingerprint) : "未知"} />
              </div>
            </section>

            <section className="risk-control-section">
              <div className="risk-section-head">
                <h3>压力与集中</h3>
                <StatusPill status={stressKnown && concentrationKnown ? "已知" : "未确认"} tone={factBoundTone(riskInputsFact, stressKnown && concentrationKnown ? "ok" : "warn", riskRequestFailed)} />
              </div>
              <div className="field-list risk-compact-fields">
                <Field label="压力损失" value={stressKnown ? metricValue(canonicalRisk.stress.lossPct, 4, "%") : "未知"} />
                <Field label="压力损失金额" value={stressKnown ? metricValue(canonicalRisk.stress.lossUsd, 2, " USD") : "未知"} />
                <Field label="压力持仓数" value={stressKnown ? metricValue(canonicalRisk.stress.positionCount, 0) : "未知"} />
                <Field label="集中度" value={concentrationKnown ? metricValue(canonicalRisk.concentration.pct, 4, "%") : "未知"} />
                <Field label="最大集中品种" value={concentrationKnown ? canonicalRisk.concentration.maxSingleName || "当前无持仓" : "未知"} />
                <Field label="适用状态" value={concentrationKnown ? (canonicalRisk.concentration.applicable ? canonicalRisk.concentration.safe ? "适用且安全" : "适用且超限" : "空仓不适用") : "未知"} />
              </div>
            </section>

            <section className="risk-control-section">
              <div className="risk-section-head">
                <h3>系统组件</h3>
                <StatusPill status={!riskDisplayable ? "未知" : hasSystemQueryError || riskBlockers.length ? "异常" : degraded.length ? "退化" : "正常"} tone={!systemFactsKnown ? "warn" : hasSystemQueryError || riskBlockers.length ? "bad" : degraded.length ? "warn" : "ok"} />
              </div>
              <div className="field-list risk-compact-fields">
                <Field label="后端就绪" value={readinessDisplayable ? (readinessReadyReported ? "是" : "否") : "未知"} tone={factBoundTone(readinessFact, readinessReadyReported ? "ok" : "warn", readinessRequestFailed)} />
                <Field label="Canonical 风险" value={riskRequestFailed ? "异常" : riskDisplayable ? canonicalRisk.schemaVersion : "未知"} tone={riskRequestFailed ? "bad" : riskKnown ? "ok" : "warn"} />
                <Field label="Readiness 风险投影" value={readinessDisplayable ? readinessRiskKnown ? "known" : pickString(readinessRisk, ["var_status"], "unknown") : "未知"} tone={factBoundTone(readinessFact, readinessRiskKnown ? "ok" : "warn", readinessRequestFailed)} />
                <Field label="就绪接口" value={readinessRequestFailed ? "异常" : readinessDisplayable ? "有事实" : "未知"} tone={readinessRequestFailed ? "bad" : readinessKnown ? "ok" : "warn"} />
                <Field label="数据库状态" value={dbDisplayable ? dbStatus : "未知"} tone={factBoundTone(dbFact, toneFromStatus(dbStatus), dbRequestFailed)} />
                <Field label="数据库总数" value={formatDecimal(dbList.length, 0)} />
                <Field label="数据库异常" value={formatDecimal(dbErrorCount, 0)} tone={dbErrorCount ? "bad" : dbKnown ? "ok" : "warn"} />
              </div>
              <div className="compact-list risk-inline-badges">
                {riskQuery.isError ? <span className="data-badge data-badge-bad">risk-summary 异常</span> : null}
                {dbQuery.isError ? <span className="data-badge data-badge-bad">db-health 异常</span> : null}
                {readinessQuery.isError ? <span className="data-badge data-badge-bad">readiness 异常</span> : null}
                {riskBlockers.length ? (
                  riskBlockers.slice(0, 5).map((item, index) => (
                    <span key={`${itemLabel(item)}-${index}`} className="data-badge data-badge-bad">{itemLabel(item)}</span>
                  ))
                ) : systemFactsKnown && !hasSystemQueryError ? (
                  <span className="data-badge data-badge-ok">无阻断组件</span>
                ) : null}
                {degraded.slice(0, 5).map((item, index) => (
                  <span key={`${itemLabel(item)}-${index}`} className="data-badge data-badge-warn">{itemLabel(item)}</span>
                ))}
              </div>
            </section>

            <section className="risk-control-section">
              <div className="risk-section-head">
                <h3>最近证据</h3>
                <StatusPill status={latestPolicyAllowed === undefined ? "暂无裁决" : latestPolicyAllowed ? "最近允许" : "最近拦截"} tone={factBoundTone(policyFact, latestPolicyAllowed === undefined ? "mute" : latestPolicyAllowed ? "ok" : "bad", policyRequestFailed)} />
              </div>
              <div className="field-list risk-compact-fields">
                <Field label="最近原因" value={latestPolicyReason} />
                <Field label="证据链结果" value={latestTraceOutcome} />
                <Field label="裁决记录" value={formatDecimal(policyItems.length, 0)} />
                <Field label="证据链" value={formatDecimal(tradeTraces.length, 0)} />
              </div>
            </section>
          </div>
        </MetricCard>

        <MetricCard title="策略裁决" className="wide-panel">
          <div className="policy-summary-strip">
            <span><b>{formatDecimal(allowed, 0)}</b> 允许</span>
            <span className={blocked ? "policy-summary-bad" : ""}><b>{formatDecimal(blocked, 0)}</b> 拦截</span>
            <span><b>{formatDecimal(totalVerdicts, 0)}</b> 总计</span>
            <span><b>{formatDecimal(allowedRate, 1)}%</b> 通过率</span>
          </div>

          {!policyItems.length ? (
            <div className="empty-state-small">最近无裁决记录</div>
          ) : (
            <div className="policy-list">
              {policyItems.slice(0, 12).map((raw, index) => {
                const item = asRecord(raw);
                const itemAllowed = pickBoolean(item, ["allowed", "ok", "pass"], false);
                const decisionId = pickString(item, ["decision_id", "id"], "");
                const action = translateDisplayValue(pickString(item, ["action", "type"], "policy"));
                const direction = translateDisplayValue(formatDirection(pick(item, ["direction", "side"])));
                const reason = translateReasonText(pickString(item, ["reason", "message"], ""));

                return (
                  <article className="policy-item" key={`${decisionId}-${index}`}>
                    <div className="policy-item-time">
                      <strong>{formatTime(pick(item, ["time", "decision_ts", "ts", "created_at"]))}</strong>
                      <span>{compactId(decisionId)}</span>
                    </div>
                    <div className="policy-item-action">
                      <StatusPill status={itemAllowed ? "允许" : "拦截"} tone={factBoundTone(policyFact, itemAllowed ? "ok" : "bad", policyRequestFailed)} />
                      <span>{action} · {direction}</span>
                    </div>
                    <div className="policy-item-reason">{reason}</div>
                  </article>
                );
              })}
            </div>
          )}
        </MetricCard>

        <MetricCard title="交易证据链" className="wide-panel">
          {!tradeTraces.length ? (
            <div className="empty-state-small">暂无交易证据链</div>
          ) : (
            <div className="trace-list">
              {tradeTraces.slice(0, 12).map((raw, index) => {
                const item = asRecord(raw);
                const rawSummary = pickString(item, ["summary_text"], "");
                const outcome = translateDisplayValue(pickString(item, ["outcome_label"], ""));
                const closeReason = translateDisplayValue(pickString(item, ["close_reason"], ""));
                const responsibility = translateDisplayValue(pickString(item, ["primary_responsibility"], ""));
                const pnl = extractTraceToken(rawSummary, "pnl");
                const primaryFactor = extractTraceToken(rawSummary, "primary_factor");
                const worstFactor = extractTraceToken(rawSummary, "worst_factor");
                const positionId = pickString(item, ["position_id", "trade_id"], "");
                const entryDecision = pickString(item, ["entry_decision_id"], "");
                const exitDecision = pickString(item, ["exit_decision_id"], "");
                const tracePnlTone = pnlTone(pnl);

                return (
                  <article className="trace-item" key={`${pickString(item, ["review_id", "position_id"], String(index))}-${index}`}>
                    <div className="trace-main">
                      <div className="trace-time">
                        <strong>{formatTime(pick(item, ["created_at", "close_ts", "exit_ts", "updated_at"]))}</strong>
                        <span>仓位 {compactId(positionId)}</span>
                      </div>
                      <div className="trace-verdict">
                        <StatusPill status={outcome} tone={factBoundTone(traceFact, traceOutcomeTone(outcome), traceRequestFailed)} />
                        <span>{closeReason}</span>
                      </div>
                      <div className="trace-responsibility">
                        <span>责任</span>
                        <strong>{responsibility}</strong>
                      </div>
                    </div>

                    <div className="trace-meta">
                      <span className="trace-chip">入 {compactId(entryDecision)}</span>
                      <span className="trace-chip">离 {compactId(exitDecision)}</span>
                      <span className={`trace-chip trace-pnl trace-pnl-${tracePnlTone === "ok" && !traceKnown ? "mute" : tracePnlTone}`}>PnL {pnl}</span>
                      <span className="trace-chip">主因 {primaryFactor}</span>
                      <span className="trace-chip">弱项 {worstFactor}</span>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
          {policyQuery.isError || tradeTracesQuery.isError ? (
            <ul className="error-list">
              {policyQuery.isError ? <li>policy-verdicts：{policyQuery.error instanceof Error ? policyQuery.error.message : "请求失败"}</li> : null}
              {tradeTracesQuery.isError ? <li>trade-trace：{tradeTracesQuery.error instanceof Error ? tradeTracesQuery.error.message : "请求失败"}</li> : null}
            </ul>
          ) : null}
        </MetricCard>
      </div>
    </section>
  );
}
