import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, FileCheck2, Gauge, ShieldAlert, ShieldCheck } from "lucide-react";
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
  const dbErrorCount = dbList.reduce<number>((acc, item) => acc + (pickArray(asRecord(item), ["errors", "issues"]).length > 0 ? 1 : 0), 0);
  const dbStatus = pickString(dbQuery.data, ["overall", "status"], "--");

  return (
    <section className="dashboard">
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
        <StatTile icon={Gauge} label="VaR" value={formatDecimal(varValue, 4)} detail={varBudget ? `限额 ${formatDecimal(varBudget, 4)}` : "未返回限额"} tone={varBudget && varValue > varBudget ? "bad" : "mute"} />
        <StatTile icon={AlertTriangle} label="阻断组件" value={formatDecimal(critical.length + blockers.length, 0)} detail={`退化 ${formatDecimal(degraded.length, 0)} · DB 异常 ${formatDecimal(dbErrorCount, 0)}`} tone={critical.length || blockers.length ? "bad" : degraded.length || dbErrorCount ? "warn" : "ok"} />
      </div>

      <div className="dashboard-grid">
        <MetricCard title="风险指标">
          <div className="field-list">
            <Field label="VaR" value={formatDecimal(varValue, 4)} />
            <Field label="VaR 限额" value={varBudget ? formatDecimal(varBudget, 4) : "--"} />
            <Field label="Kelly" value={formatDecimal(kellyFraction, 4)} />
            <Field label="Kelly 预算" value={formatDecimal(kellyBudget, 4)} />
            <Field label="Stress VaR" value={formatDecimal(stressVaR, 4)} />
            <Field label="Stress 回撤" value={formatDecimal(stressDrop, 4)} />
            <Field label="单品种权重" value={formatDecimal(concentrationMax, 4)} />
            <Field label="行业集中度" value={formatDecimal(concentrationSector, 4)} />
          </div>
        </MetricCard>

        <MetricCard title="系统组件">
          <div className="field-list">
            <Field label="交易阻断" value={riskBlocked ? "是" : "否"} tone={riskBlocked ? "bad" : "ok"} />
            <Field label="后端就绪" value={readinessReady ? "是" : "否"} tone={readinessReady ? "ok" : "warn"} />
            <Field label="合约版本" value={schema} />
            <Field label="数据库状态" value={dbStatus} tone={toneFromStatus(dbStatus)} />
            <Field label="数据库异常" value={formatDecimal(dbErrorCount, 0)} tone={dbErrorCount ? "bad" : "ok"} />
          </div>
          <div className="compact-list">
            {[...critical, ...blockers].length ? (
              [...critical, ...blockers].slice(0, 8).map((item, index) => (
                <span key={`${itemLabel(item)}-${index}`} className="data-badge data-badge-bad">{itemLabel(item)}</span>
              ))
            ) : (
              <span className="data-badge data-badge-ok">无阻断组件</span>
            )}
            {degraded.slice(0, 8).map((item, index) => (
              <span key={`${itemLabel(item)}-${index}`} className="data-badge data-badge-warn">{itemLabel(item)}</span>
            ))}
          </div>
        </MetricCard>

        <MetricCard title="策略裁决" className="wide-panel">
          <div className="performance-row">
            <div>
              <span>允许</span>
              <strong>{formatDecimal(allowed, 0)}</strong>
            </div>
            <div>
              <span>拦截</span>
              <strong>{formatDecimal(blocked, 0)}</strong>
            </div>
            <div>
              <span>总计</span>
              <strong>{formatDecimal(totalVerdicts, 0)}</strong>
            </div>
            <div>
              <span>通过率</span>
              <strong>{formatDecimal(allowedRate, 1)}%</strong>
            </div>
          </div>

          <div className="table-wrap table-spaced">
            <table className="mobile-card-table policy-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>裁决</th>
                  <th>动作</th>
                  <th>方向</th>
                  <th>结果</th>
                  <th>原因</th>
                </tr>
              </thead>
              <tbody>
                {policyItems.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="empty-state-small">最近无裁决记录</td>
                  </tr>
                ) : null}
                {policyItems.slice(0, 30).map((raw, index) => {
                  const item = asRecord(raw);
                  const itemAllowed = pickBoolean(item, ["allowed", "ok", "pass"], false);
                  return (
                    <tr key={`${pickString(item, ["decision_id", "id"], String(index))}-${index}`}>
                      <td>{formatTime(pick(item, ["time", "decision_ts", "ts", "created_at"]))}</td>
                      <td>{pickString(item, ["decision_id", "id"], "--")}</td>
                      <td>{translateDisplayValue(pickString(item, ["action", "type"], "policy"))}</td>
                      <td>{translateDisplayValue(formatDirection(pick(item, ["direction", "side"])))}</td>
                      <td><StatusPill status={itemAllowed ? "允许" : "拦截"} tone={itemAllowed ? "ok" : "bad"} /></td>
                      <td>{translateReasonText(pickString(item, ["reason", "message"], "--"))}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </MetricCard>

        <MetricCard title="数据健康" className="wide-panel">
          <div className="field-list">
            <Field label="数据库健康" value={dbStatus} tone={toneFromStatus(dbStatus)} />
            <Field label="数据库总数" value={formatDecimal(dbList.length, 0)} />
            <Field label="数据库异常项" value={formatDecimal(dbErrorCount, 0)} tone={dbErrorCount ? "bad" : "ok"} />
            <Field label="风险接口" value={riskQuery.isError ? "异常" : "正常"} tone={riskQuery.isError ? "bad" : "ok"} />
            <Field label="就绪接口" value={readinessQuery.isError ? "异常" : "正常"} tone={readinessQuery.isError ? "bad" : "ok"} />
          </div>
          {riskQuery.isError || dbQuery.isError || readinessQuery.isError ? (
            <ul className="error-list">
              {riskQuery.isError ? <li>risk-summary：{riskQuery.error instanceof Error ? riskQuery.error.message : "请求失败"}</li> : null}
              {dbQuery.isError ? <li>db-health：{dbQuery.error instanceof Error ? dbQuery.error.message : "请求失败"}</li> : null}
              {readinessQuery.isError ? <li>backend-readiness：{readinessQuery.error instanceof Error ? readinessQuery.error.message : "请求失败"}</li> : null}
            </ul>
          ) : null}
          <p className="summary-note"><FileCheck2 size={14} /> 需要更底层排查时，从运维页看数据库和 readiness 明细。</p>
        </MetricCard>

        <MetricCard title="交易证据链" className="wide-panel">
          <div className="table-wrap">
            <table className="mobile-card-table trade-traces-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>仓位</th>
                  <th>入场裁决</th>
                  <th>离场裁决</th>
                  <th>结果</th>
                  <th>关闭原因</th>
                  <th>责任</th>
                  <th>摘要</th>
                </tr>
              </thead>
              <tbody>
                {!tradeTraces.length ? (
                  <tr><td colSpan={8} className="empty-state-small">暂无交易证据链</td></tr>
                ) : null}
                {tradeTraces.slice(0, 12).map((raw, index) => {
                  const item = asRecord(raw);
                  return (
                    <tr key={`${pickString(item, ["review_id", "position_id"], String(index))}-${index}`}>
                      <td>{formatTime(pick(item, ["created_at", "close_ts", "exit_ts", "updated_at"]))}</td>
                      <td>{pickString(item, ["position_id", "trade_id"], "--")}</td>
                      <td>{pickString(item, ["entry_decision_id"], "--")}</td>
                      <td>{pickString(item, ["exit_decision_id"], "--")}</td>
                      <td>{translateDisplayValue(pickString(item, ["outcome_label"], "--"))}</td>
                      <td>{translateDisplayValue(pickString(item, ["close_reason"], "--"))}</td>
                      <td>{translateDisplayValue(pickString(item, ["primary_responsibility"], "--"))}</td>
                      <td>{translateReasonText(pickString(item, ["summary_text"], "--"))}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
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
