import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Gauge, ShieldAlert, ShieldCheck } from "lucide-react";
import { MetricCard } from "@/components/Card";
import { Field, StatTile, toneFromStatus } from "@/components/DashboardBits";
import { StatusPill } from "@/components/StatusPill";
import { useAuth } from "@/contexts/AuthContext";
import { useLiveState } from "@/hooks/useLiveState";
import {
  getBackendReadiness,
  getRecentTradeTraces,
  getRiskPolicyVerdicts,
  getRiskSummary,
  getSystemDbHealth,
} from "@/api/client";
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

function itemLabel(value: unknown): string {
  const item = asRecord(value);
  return translateDisplayValue(pickString(item, ["name", "component", "id", "key"], typeof value === "string" ? value : "--"));
}

function mergeRiskSummary(base: unknown, live: unknown): Record<string, unknown> {
  const baseRecord = asRecord(base);
  const liveRecord = asRecord(live);
  const merged: Record<string, unknown> = { ...baseRecord, ...liveRecord };
  for (const key of ["var", "kelly", "stress", "concentration", "policy", "system_health"]) {
    const liveChild = asRecord(liveRecord[key]);
    if (!Object.keys(liveChild).length && baseRecord[key]) {
      merged[key] = baseRecord[key];
    }
  }
  return merged;
}

function percentTone(value: number): "ok" | "warn" | "bad" | "mute" {
  if (!Number.isFinite(value) || value <= 0) return "mute";
  if (value >= 100) return "bad";
  if (value >= 70) return "warn";
  return "ok";
}

function RiskMiniMetric({
  label,
  value,
  detail,
  tone = "mute",
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: "ok" | "warn" | "bad" | "mute";
}) {
  return (
    <div className={`risk-mini-metric risk-mini-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail ? <small>{detail}</small> : null}
    </div>
  );
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
  if (!value || value === "--") return "--";
  if (value.length <= 12) return value;
  return `${value.slice(0, 8)}...${value.slice(-4)}`;
}

function extractTraceToken(summary: string, key: string): string {
  if (!summary || summary === "--") return "--";
  const match = summary.match(new RegExp(`${key}=([^;]+)`));
  return match ? translateReasonText(match[1].trim()) : "--";
}

function traceOutcomeTone(outcome: string): "ok" | "warn" | "bad" | "mute" {
  if (!outcome || outcome === "--") return "mute";
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
  const { authenticated } = useAuth();
  const { snapshot } = useLiveState({ enabled: authenticated });
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
  const readinessQuery = useQuery({
    queryKey: ["backend-readiness", "risk"],
    queryFn: getBackendReadiness,
    refetchInterval: 15_000,
    staleTime: 5_000,
  });
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

  const risk = mergeRiskSummary(riskQuery.data, pick(snapshot, ["risk"]));
  const varSummary = pickRecord(risk, ["var"]) || {};
  const kellySummary = pickRecord(risk, ["kelly"]) || {};
  const stressSummary = pickRecord(risk, ["stress"]) || {};
  const concentrationSummary = pickRecord(risk, ["concentration"]) || {};
  const policy = Object.keys(asRecord(policyQuery.data)).length ? asRecord(policyQuery.data) : pickRecord(risk, ["policy"]) || {};
  const systemHealth = pickRecord(risk, ["system_health"]) || {};

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

  const riskHealth = pickString(systemHealth, ["overall", "status", "state"], "unknown");
  const riskBlocked = pickBoolean(systemHealth, ["trading_blocked", "blocked"], false);
  const critical = pickArray(systemHealth, ["critical_components", "blocking_components"]);
  const degraded = pickArray(systemHealth, ["degraded_components"]);
  const impact = pickString(systemHealth, ["impact_summary", "status", "summary"], "--");
  const readinessReady = pickBoolean(readinessQuery.data, ["ready_for_frontend", "ready", "ok"], false);
  const schema = pickString(readinessQuery.data, ["schema_version", "version"], "--");
  const blockers = pickArray(readinessQuery.data, ["blockers"]);

  const dbList = pickArray(dbQuery.data, ["databases", "database_list", "items"]);
  const dbErrorCount = dbList.reduce<number>((acc, item) => {
    const row = asRecord(item);
    const freshness = pickString(row, ["freshness", "status", "state"], "");
    const exists = pickBoolean(row, ["exists"], true);
    const hasIssues = pickArray(row, ["errors", "issues"]).length > 0;
    return acc + (!exists || hasIssues || ["missing", "stale", "old", "error"].includes(freshness) ? 1 : 0);
  }, 0);
  const dbStatus = pickString(dbQuery.data, ["overall", "status"], "--");
  const riskBlockers = [...critical, ...blockers];
  const latestPolicy = asRecord(policyItems[0]);
  const latestTrace = asRecord(tradeTraces[0]);
  const latestPolicyAllowed = policyItems.length ? pickBoolean(latestPolicy, ["allowed", "ok", "pass"], false) : undefined;
  const latestPolicyReason = policyItems.length ? translateReasonText(pickString(latestPolicy, ["reason", "message"], "--")) : "--";
  const latestTraceOutcome = tradeTraces.length ? translateDisplayValue(pickString(latestTrace, ["outcome_label"], "--")) : "--";
  const hasSystemQueryError = riskQuery.isError || dbQuery.isError || readinessQuery.isError;

  return (
    <section className="dashboard risk-dashboard">
      <div className="dashboard-header">
        <div>
          <div className="eyebrow">风控审计</div>
          <h1>风控审计</h1>
          <p>展示风险引擎状态、限额占用、Policy 裁决和阻断组件。</p>
        </div>
        <div className="header-status">
          <StatusPill status={`风险 ${riskHealth}`} tone={toneFromStatus(riskHealth)} />
          <StatusPill status={riskBlocked ? "交易阻断" : "交易可行"} tone={riskBlocked ? "bad" : "ok"} />
          <StatusPill status={readinessReady ? "后端就绪" : "后端受限"} tone={readinessReady ? "ok" : "warn"} />
        </div>
      </div>

      <div className="stat-grid">
        <StatTile icon={ShieldCheck} label="系统健康" value={translateDisplayValue(riskHealth)} detail={translateDisplayValue(impact)} tone={toneFromStatus(riskHealth)} />
        <StatTile icon={ShieldAlert} label="策略拦截" value={formatDecimal(blocked, 0)} detail={`允许 ${formatDecimal(allowed, 0)} · 通过率 ${formatDecimal(allowedRate, 1)}%`} tone={blocked ? "warn" : "ok"} />
        <StatTile icon={Gauge} label="VaR" value={varHasData ? formatDecimal(varValue, 4) : "未接入"} detail={varHasData ? (varBudget ? `限额 ${formatDecimal(varBudget, 4)}` : "未返回限额") : "summary 未传权益曲线"} tone={varBudget && varValue > varBudget ? "bad" : "mute"} />
        <StatTile icon={AlertTriangle} label="阻断组件" value={formatDecimal(critical.length + blockers.length, 0)} detail={`退化 ${formatDecimal(degraded.length, 0)} · DB 异常 ${formatDecimal(dbErrorCount, 0)}`} tone={critical.length || blockers.length ? "bad" : degraded.length || dbErrorCount ? "warn" : "ok"} />
      </div>

      <div className="dashboard-grid">
        <MetricCard title="风险控制面板" className="wide-panel risk-control-overview">
          <div className="risk-mini-grid">
            <RiskMiniMetric label="交易闸门" value={riskBlocked ? "阻断" : "放行"} detail={translateDisplayValue(impact)} tone={riskBlocked ? "bad" : "ok"} />
            <RiskMiniMetric label="VaR 占用" value={varHasData && varBudget ? `${formatDecimal(varUsage, 1)}%` : "未接入"} detail={varHasData ? `${formatDecimal(varValue, 4)} / ${varBudget ? formatDecimal(varBudget, 4) : "--"}` : "等待权益曲线"} tone={varHasData ? percentTone(varUsage) : "mute"} />
            <RiskMiniMetric label="Kelly 占用" value={kellyHasData ? (kellyBudget ? `${formatDecimal(kellyUsage, 1)}%` : formatDecimal(kellyFraction, 4)) : "未接入"} detail={kellyHasData ? `预算 ${kellyBudget ? formatDecimal(kellyBudget, 4) : "--"}` : "等待交易胜率"} tone={kellyHasData ? percentTone(kellyUsage) : "mute"} />
            <RiskMiniMetric label="Policy 通过率" value={`${formatDecimal(allowedRate, 1)}%`} detail={`允许 ${formatDecimal(allowed, 0)} / 拦截 ${formatDecimal(blocked, 0)}`} tone={blocked ? "warn" : "ok"} />
            <RiskMiniMetric label="组件异常" value={formatDecimal(riskBlockers.length + degraded.length, 0)} detail={`阻断 ${formatDecimal(riskBlockers.length, 0)} · 退化 ${formatDecimal(degraded.length, 0)}`} tone={riskBlockers.length ? "bad" : degraded.length ? "warn" : "ok"} />
            <RiskMiniMetric label="数据健康" value={dbQuery.isError ? "接口异常" : dbErrorCount ? `${formatDecimal(dbErrorCount, 0)} 异常` : translateDisplayValue(dbStatus)} detail={`库 ${formatDecimal(dbList.length, 0)} · 合约 ${schema}`} tone={dbQuery.isError || dbErrorCount ? "bad" : toneFromStatus(dbStatus)} />
          </div>

          <div className="risk-control-grid">
            <section className="risk-control-section">
              <div className="risk-section-head">
                <h3>限额占用</h3>
                <StatusPill status={varHasData || kellyHasData || concentrationHasData ? (varUsage >= 100 || kellyUsage >= 100 ? "超限" : "正常") : "未接入"} tone={varHasData || kellyHasData || concentrationHasData ? (varUsage >= 100 || kellyUsage >= 100 ? "bad" : "ok") : "mute"} />
              </div>
              <RiskBar label="VaR" value={varUsage} valueLabel={varHasData ? undefined : "--"} detail={varHasData ? `当前 ${formatDecimal(varValue, 4)} · 限额 ${varBudget ? formatDecimal(varBudget, 4) : "--"}` : "等待权益曲线输入"} tone={varHasData ? percentTone(varUsage) : "mute"} />
              <RiskBar label="Kelly" value={kellyUsage} valueLabel={kellyHasData ? undefined : "--"} detail={kellyHasData ? `当前 ${formatDecimal(kellyFraction, 4)} · 预算 ${kellyBudget ? formatDecimal(kellyBudget, 4) : "--"}` : "等待交易胜率与盈亏样本"} tone={kellyHasData ? percentTone(kellyUsage) : "mute"} />
              <RiskBar label="集中度" value={concentrationUsage} valueLabel={concentrationHasData ? undefined : "--"} detail={concentrationHasData ? `单品种 ${formatDecimal(concentrationMax, 4)} · 行业 ${formatDecimal(concentrationSector, 4)}` : "等待仓位/因子权重"} tone={concentrationHasData ? percentTone(concentrationUsage) : "mute"} />
            </section>

            <section className="risk-control-section">
              <div className="risk-section-head">
                <h3>压力与集中</h3>
                <StatusPill status={stressHasData ? "有数据" : "未接入"} tone={stressHasData ? "ok" : "mute"} />
              </div>
              <div className="field-list risk-compact-fields">
                <Field label="Stress VaR" value={stressHasData ? formatDecimal(stressVaR, 4) : "等待权益曲线"} />
                <Field label="Stress 回撤" value={stressHasData ? formatDecimal(stressDrop, 4) : "等待压力测试"} />
                <Field label="单品种权重" value={concentrationHasData ? formatDecimal(concentrationMax, 4) : "等待权重"} />
                <Field label="行业集中度" value={concentrationHasData ? formatDecimal(concentrationSector, 4) : "等待权重"} />
              </div>
            </section>

            <section className="risk-control-section">
              <div className="risk-section-head">
                <h3>系统组件</h3>
                <StatusPill status={hasSystemQueryError || riskBlockers.length ? "异常" : degraded.length ? "退化" : "正常"} tone={hasSystemQueryError || riskBlockers.length ? "bad" : degraded.length ? "warn" : "ok"} />
              </div>
              <div className="field-list risk-compact-fields">
                <Field label="后端就绪" value={readinessReady ? "是" : "否"} tone={readinessReady ? "ok" : "warn"} />
                <Field label="风险接口" value={riskQuery.isError ? "异常" : "正常"} tone={riskQuery.isError ? "bad" : "ok"} />
                <Field label="就绪接口" value={readinessQuery.isError ? "异常" : "正常"} tone={readinessQuery.isError ? "bad" : "ok"} />
                <Field label="数据库状态" value={dbStatus} tone={toneFromStatus(dbStatus)} />
                <Field label="数据库总数" value={formatDecimal(dbList.length, 0)} />
                <Field label="数据库异常" value={formatDecimal(dbErrorCount, 0)} tone={dbErrorCount ? "bad" : "ok"} />
              </div>
              <div className="compact-list risk-inline-badges">
                {riskQuery.isError ? <span className="data-badge data-badge-bad">risk-summary 异常</span> : null}
                {dbQuery.isError ? <span className="data-badge data-badge-bad">db-health 异常</span> : null}
                {readinessQuery.isError ? <span className="data-badge data-badge-bad">readiness 异常</span> : null}
                {riskBlockers.length ? (
                  riskBlockers.slice(0, 5).map((item, index) => (
                    <span key={`${itemLabel(item)}-${index}`} className="data-badge data-badge-bad">{itemLabel(item)}</span>
                  ))
                ) : !hasSystemQueryError ? (
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
                <StatusPill status={latestPolicyAllowed === undefined ? "暂无裁决" : latestPolicyAllowed ? "最近允许" : "最近拦截"} tone={latestPolicyAllowed === undefined ? "mute" : latestPolicyAllowed ? "ok" : "bad"} />
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
                const decisionId = pickString(item, ["decision_id", "id"], "--");
                const action = translateDisplayValue(pickString(item, ["action", "type"], "policy"));
                const direction = translateDisplayValue(formatDirection(pick(item, ["direction", "side"])));
                const reason = translateReasonText(pickString(item, ["reason", "message"], "--"));

                return (
                  <article className="policy-item" key={`${decisionId}-${index}`}>
                    <div className="policy-item-time">
                      <strong>{formatTime(pick(item, ["time", "decision_ts", "ts", "created_at"]))}</strong>
                      <span>{compactId(decisionId)}</span>
                    </div>
                    <div className="policy-item-action">
                      <StatusPill status={itemAllowed ? "允许" : "拦截"} tone={itemAllowed ? "ok" : "bad"} />
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
                const rawSummary = pickString(item, ["summary_text"], "--");
                const outcome = translateDisplayValue(pickString(item, ["outcome_label"], "--"));
                const closeReason = translateDisplayValue(pickString(item, ["close_reason"], "--"));
                const responsibility = translateDisplayValue(pickString(item, ["primary_responsibility"], "--"));
                const pnl = extractTraceToken(rawSummary, "pnl");
                const primaryFactor = extractTraceToken(rawSummary, "primary_factor");
                const worstFactor = extractTraceToken(rawSummary, "worst_factor");
                const positionId = pickString(item, ["position_id", "trade_id"], "--");
                const entryDecision = pickString(item, ["entry_decision_id"], "--");
                const exitDecision = pickString(item, ["exit_decision_id"], "--");

                return (
                  <article className="trace-item" key={`${pickString(item, ["review_id", "position_id"], String(index))}-${index}`}>
                    <div className="trace-main">
                      <div className="trace-time">
                        <strong>{formatTime(pick(item, ["created_at", "close_ts", "exit_ts", "updated_at"]))}</strong>
                        <span>仓位 {compactId(positionId)}</span>
                      </div>
                      <div className="trace-verdict">
                        <StatusPill status={outcome} tone={traceOutcomeTone(outcome)} />
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
                      <span className={`trace-chip trace-pnl trace-pnl-${pnlTone(pnl)}`}>PnL {pnl}</span>
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
