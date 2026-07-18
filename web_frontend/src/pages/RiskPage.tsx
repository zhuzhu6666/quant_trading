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
  pickRecord,
  pickString,
} from "@/lib/compat";
import { translateDisplayValue, translateReasonText } from "@/lib/display";
import { formatDecimal, formatTime } from "@/lib/format";
import { useBackendReadinessQuery } from "@/hooks/useCoreQueries";
import { factBoundTone, factHasDisplayValue, factIsKnown, readFact } from "@/api/fact";

function itemLabel(value: unknown): string {
  const item = asRecord(value);
  return translateDisplayValue(pickString(item, ["name", "component", "id", "key"], typeof value === "string" ? value : ""));
}

function percentTone(value: number): "ok" | "warn" | "bad" | "mute" {
  if (!Number.isFinite(value) || value <= 0) return "mute";
  if (value >= 100) return "bad";
  if (value >= 70) return "warn";
  return "ok";
}

function RiskBar({
  label,
  value,
  valueLabel,
  detail,
  tone,
}: {
  label: string;
  value: number;
  valueLabel?: string;
  detail: string;
  tone: "ok" | "warn" | "bad" | "mute";
}) {
  const width = Math.min(Math.max(value, 0), 100);
  return (
    <div className={`risk-bar risk-bar-${tone}`}>
      <div className="risk-bar-head">
        <span>{label}</span>
        <strong>{valueLabel ?? `${formatDecimal(value, 1)}%`}</strong>
      </div>
      <div className="risk-bar-track" aria-label={`${label} ${valueLabel ?? `${formatDecimal(value, 1)}%`}`}>
        <i style={{ width: `${width}%` }} />
      </div>
      <small>{detail}</small>
    </div>
  );
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
  const varSummary = pickRecord(risk, ["var"]) || {};
  const kellySummary = pickRecord(risk, ["kelly"]) || {};
  const stressSummary = pickRecord(risk, ["stress"]) || {};
  const concentrationSummary = pickRecord(risk, ["concentration"]) || {};
  // The dedicated policy endpoint is the only source for this panel. Falling
  // back to risk.summary would bind one endpoint's data to another endpoint's
  // freshness and could turn an old nested projection green.
  const policy = asRecord(policyQuery.data);
  const systemHealth = pickRecord(risk, ["system_health"]) || {};
  const riskFact = readFact(riskQuery.data, "risk.summary.v2");
  const dbFact = readFact(dbQuery.data, "system.db-health.v2");
  const readinessFact = readFact(readinessQuery.data, "ops.backend-readiness.v2");
  const policyFact = readFact(policyQuery.data, "risk.policy-verdicts.v2");
  const traceFact = readFact(tradeTracesQuery.data, "risk.trade-trace-recent.v2");
  const riskRequestFailed = riskQuery.isError || riskQuery.isRefetchError;
  const dbRequestFailed = dbQuery.isError || dbQuery.isRefetchError;
  const readinessRequestFailed = readinessQuery.isError || readinessQuery.isRefetchError;
  const policyRequestFailed = policyQuery.isError || policyQuery.isRefetchError;
  const traceRequestFailed = tradeTracesQuery.isError || tradeTracesQuery.isRefetchError;
  const riskKnown = factIsKnown(riskFact, riskRequestFailed);
  const dbKnown = factIsKnown(dbFact, dbRequestFailed);
  const readinessKnown = factIsKnown(readinessFact, readinessRequestFailed);
  const traceKnown = factIsKnown(traceFact, traceRequestFailed);
  const riskDisplayable = factHasDisplayValue(riskFact);
  const dbDisplayable = factHasDisplayValue(dbFact);
  const readinessDisplayable = factHasDisplayValue(readinessFact);

  const varValue = pickNumber(varSummary, ["var", "var_pct", "value", "var_95"], 0);
  const varBudget = pickNumber(varSummary, ["limit", "max", "value_limit", "var_limit"], 0);
  const kellyFraction = pickNumber(kellySummary, ["kelly_fraction", "fraction", "value"], 0);
  const kellyBudget = pickNumber(kellySummary, ["position_fraction", "max_fraction", "position_budget", "half_kelly", "quarter_kelly"], 0);
  const stressVaR = pickNumber(stressSummary, ["var", "value", "stress_var"], 0);
  const stressDrop = pickNumber(stressSummary, ["max_drawdown_pct", "max_drawdown", "drawdown", "stress"], 0);
  const concentrationMax = pickNumber(concentrationSummary, ["max_single_weight", "max_weight", "concentration"], 0);
  const concentrationSector = pickNumber(concentrationSummary, ["max_sector_weight", "sector_weight"], 0);
  const varUsage = varBudget > 0 ? (varValue / varBudget) * 100 : 0;
  const kellyUsage = kellyBudget > 0 ? (kellyFraction / kellyBudget) * 100 : 0;
  const concentrationUsage = Math.max(concentrationMax, concentrationSector) * 100;
  const varStatus = pickString(varSummary, ["status"], "");
  const kellyStatus = pickString(kellySummary, ["status"], "");
  const stressStatus = pickString(stressSummary, ["status"], "");
  const concentrationStatus = pickString(concentrationSummary, ["status"], "");
  const varHasData = varStatus !== "no data" && (varValue > 0 || varBudget > 0 || pickNumber(varSummary, ["lookback"], 0) > 0);
  const kellyHasData = kellyStatus !== "no data" && (kellyFraction > 0 || pickNumber(kellySummary, ["win_rate"], 0) > 0 || pickNumber(kellySummary, ["avg_loss"], 0) > 0);
  const stressHasData = stressStatus !== "no data" && (stressDrop > 0 || stressVaR > 0 || pickArray(stressSummary, ["scenarios"]).length > 0);
  const concentrationHasData = concentrationStatus !== "no data" && (concentrationMax > 0 || concentrationSector > 0);

  const policyCounts = pickRecord(policy, ["counts"]) || {};
  const allowed = pickNumber(policyCounts, ["allowed"], 0);
  const blocked = pickNumber(policyCounts, ["blocked"], 0);
  const totalVerdicts = allowed + blocked;
  const allowedRate = totalVerdicts ? (allowed / totalVerdicts) * 100 : 0;
  const policyItems = useMemo(() => pickArray(policy, ["items", "recent_items", "decisions", "history"]), [policy]);
  const tradeTraces = useMemo(() => pickArray(tradeTracesQuery.data, ["items", "traces", "rows"]), [tradeTracesQuery.data]);

  const riskHealth = pickString(systemHealth, ["overall", "status", "state"], "");
  const riskBlocked = pickBoolean(systemHealth, ["trading_blocked", "blocked"], false);
  const critical = pickArray(systemHealth, ["critical_components", "blocking_components"]);
  const degraded = pickArray(systemHealth, ["degraded_components"]);
  const impact = pickString(systemHealth, ["impact_summary", "status", "summary"], "");
  const readinessReadyReported = pickBoolean(readinessQuery.data, ["ready_for_frontend", "ready", "ok"], false);
  const schema = pickString(readinessQuery.data, ["schema_version", "version"], "");
  const blockers = pickArray(readinessQuery.data, ["blockers"]);

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
  const systemFactsKnown = riskKnown && dbKnown && readinessKnown;

  return (
    <section className="dashboard risk-dashboard">
      <div className="dashboard-header">
        <div>
          <div className="eyebrow">风控审计</div>
          <h1>风控审计</h1>
          <p>展示风险引擎状态、限额占用、策略裁决和阻断组件。</p>
        </div>
        <div className="header-status">
          <StatusPill status={riskDisplayable ? `风险 ${riskHealth}` : "风险状态未知"} tone={factBoundTone(riskFact, toneFromStatus(riskHealth), riskRequestFailed)} />
          <StatusPill status={riskDisplayable ? (riskBlocked ? "交易阻断" : "交易可行") : "交易许可未知"} tone={factBoundTone(riskFact, riskBlocked ? "bad" : "ok", riskRequestFailed)} />
          <StatusPill status={readinessDisplayable ? (readinessReadyReported ? "后端就绪" : "后端受限") : "后端就绪状态未知"} tone={factBoundTone(readinessFact, readinessReadyReported ? "ok" : "warn", readinessRequestFailed)} />
        </div>
      </div>

      <div className="stat-grid">
        <StatTile icon={ShieldCheck} label="系统健康" value={riskDisplayable ? translateDisplayValue(riskHealth) : "未知"} detail={translateDisplayValue(impact)} tone={factBoundTone(riskFact, toneFromStatus(riskHealth), riskRequestFailed)} />
        <StatTile icon={ShieldAlert} label="策略拦截" value={formatDecimal(blocked, 0)} detail={`允许 ${formatDecimal(allowed, 0)} · 通过率 ${formatDecimal(allowedRate, 1)}%`} tone={factBoundTone(policyFact, blocked ? "warn" : "ok", policyRequestFailed)} />
        {varHasData ? <StatTile icon={Gauge} label="VaR" value={formatDecimal(varValue, 4)} detail={varBudget ? `限额 ${formatDecimal(varBudget, 4)}` : undefined} tone={varBudget && varValue > varBudget ? "bad" : "mute"} /> : null}
        <StatTile icon={AlertTriangle} label="阻断组件" value={formatDecimal(critical.length + blockers.length, 0)} detail={`退化 ${formatDecimal(degraded.length, 0)} · DB 异常 ${formatDecimal(dbErrorCount, 0)}`} tone={!systemFactsKnown ? "warn" : critical.length || blockers.length ? "bad" : degraded.length || dbErrorCount ? "warn" : "ok"} />
      </div>

      <div className="dashboard-grid">
        <MetricCard title="风险控制面板" className="wide-panel risk-control-overview">
          <div className="risk-mini-grid">
            <RiskMiniMetric label="交易闸门" value={riskDisplayable ? (riskBlocked ? "阻断" : "放行") : "未知"} detail={translateDisplayValue(impact)} tone={factBoundTone(riskFact, riskBlocked ? "bad" : "ok", riskRequestFailed)} />
            {varHasData ? <RiskMiniMetric label="VaR 占用" value={varBudget ? `${formatDecimal(varUsage, 1)}%` : formatDecimal(varValue, 4)} detail={varBudget ? `${formatDecimal(varValue, 4)} / ${formatDecimal(varBudget, 4)}` : undefined} tone={riskKnown ? percentTone(varUsage) : "warn"} /> : null}
            {kellyHasData ? <RiskMiniMetric label="Kelly 占用" value={kellyBudget ? `${formatDecimal(kellyUsage, 1)}%` : formatDecimal(kellyFraction, 4)} detail={kellyBudget ? `预算 ${formatDecimal(kellyBudget, 4)}` : undefined} tone={riskKnown ? percentTone(kellyUsage) : "warn"} /> : null}
            <RiskMiniMetric label="策略通过率" value={`${formatDecimal(allowedRate, 1)}%`} detail={`允许 ${formatDecimal(allowed, 0)} / 拦截 ${formatDecimal(blocked, 0)}`} tone={factBoundTone(policyFact, blocked ? "warn" : "ok", policyRequestFailed)} />
            <RiskMiniMetric label="组件异常" value={formatDecimal(riskBlockers.length + degraded.length, 0)} detail={`阻断 ${formatDecimal(riskBlockers.length, 0)} · 退化 ${formatDecimal(degraded.length, 0)}`} tone={!systemFactsKnown ? "warn" : riskBlockers.length ? "bad" : degraded.length ? "warn" : "ok"} />
            <RiskMiniMetric label="数据健康" value={!dbDisplayable ? "未知" : dbErrorCount ? `${formatDecimal(dbErrorCount, 0)} 异常` : translateDisplayValue(dbStatus)} detail={`库 ${formatDecimal(dbList.length, 0)} · 合约 ${schema}${dbRequestFailed ? " · 接口异常" : ""}`} tone={factBoundTone(dbFact, dbErrorCount ? "bad" : toneFromStatus(dbStatus), dbRequestFailed)} />
          </div>

          <div className="risk-control-grid">
            <section className="risk-control-section">
              <div className="risk-section-head">
                <h3>限额占用</h3>
                <StatusPill status={riskKnown && (varHasData || kellyHasData || concentrationHasData) ? (varUsage >= 100 || kellyUsage >= 100 ? "超限" : "正常") : "未确认"} tone={!riskKnown ? "warn" : varHasData || kellyHasData || concentrationHasData ? (varUsage >= 100 || kellyUsage >= 100 ? "bad" : "ok") : "mute"} />
              </div>
              {varHasData && varBudget ? <RiskBar label="VaR" value={varUsage} detail={`当前 ${formatDecimal(varValue, 4)} · 限额 ${formatDecimal(varBudget, 4)}`} tone={riskKnown ? percentTone(varUsage) : "warn"} /> : null}
              {kellyHasData && kellyBudget ? <RiskBar label="Kelly" value={kellyUsage} detail={`当前 ${formatDecimal(kellyFraction, 4)} · 预算 ${formatDecimal(kellyBudget, 4)}`} tone={riskKnown ? percentTone(kellyUsage) : "warn"} /> : null}
              {concentrationHasData ? <RiskBar label="集中度" value={concentrationUsage} detail={`单品种 ${formatDecimal(concentrationMax, 4)} · 行业 ${formatDecimal(concentrationSector, 4)}`} tone={riskKnown ? percentTone(concentrationUsage) : "warn"} /> : null}
            </section>

            <section className="risk-control-section">
              <div className="risk-section-head">
                <h3>压力与集中</h3>
                <StatusPill status={riskKnown ? (stressHasData ? "有数据" : "未接入") : "未确认"} tone={!riskKnown ? "warn" : stressHasData ? "ok" : "mute"} />
              </div>
              <div className="field-list risk-compact-fields">
                {stressHasData ? <Field label="压力 VaR" value={formatDecimal(stressVaR, 4)} /> : null}
                {stressHasData ? <Field label="压力回撤" value={formatDecimal(stressDrop, 4)} /> : null}
                <Field label="单品种权重" value={concentrationHasData ? formatDecimal(concentrationMax, 4) : "等待权重"} />
                <Field label="行业集中度" value={concentrationHasData ? formatDecimal(concentrationSector, 4) : "等待权重"} />
              </div>
            </section>

            <section className="risk-control-section">
              <div className="risk-section-head">
                <h3>系统组件</h3>
                <StatusPill status={!riskDisplayable ? "未知" : hasSystemQueryError || riskBlockers.length ? "异常" : degraded.length ? "退化" : "正常"} tone={!systemFactsKnown ? "warn" : hasSystemQueryError || riskBlockers.length ? "bad" : degraded.length ? "warn" : "ok"} />
              </div>
              <div className="field-list risk-compact-fields">
                <Field label="后端就绪" value={readinessDisplayable ? (readinessReadyReported ? "是" : "否") : "未知"} tone={factBoundTone(readinessFact, readinessReadyReported ? "ok" : "warn", readinessRequestFailed)} />
                <Field label="风险接口" value={riskRequestFailed ? "异常" : riskDisplayable ? "有事实" : "未知"} tone={riskRequestFailed ? "bad" : riskKnown ? "ok" : "warn"} />
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
