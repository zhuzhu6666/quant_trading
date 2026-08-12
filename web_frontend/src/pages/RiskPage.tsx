import { useMemo, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Gauge, ShieldAlert, ShieldCheck } from "lucide-react";
import { MetricCard } from "@/components/Card";
import { CompactMetric as RiskMiniMetric, Field, StatTile, toneFromStatus } from "@/components/DashboardBits";
import { StatusPill } from "@/components/StatusPill";
import {
  getRecentTradeTraces,
  getRiskPolicyVerdicts,
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
import { formatDecimal, formatTime, formatTimeRange } from "@/lib/format";
import { useBackendReadinessQuery } from "@/hooks/useCoreQueries";
import { factBoundTone, factHasDisplayValue, factIsKnown, factStatusLabel, readFact, readFactComponent } from "@/api/fact";
import { decodeCanonicalRiskSnapshot, knownMetric } from "@/api/riskSnapshot";
import { queryKeys } from "@/api/queryKeys";

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

type ChainTone = "ok" | "warn" | "bad" | "mute";

type ExecutionChainRow = {
  key: string;
  sortTime: number;
  timeValue: unknown;
  factorSignals: Array<Record<string, unknown>>;
  admissionSkips: Array<Record<string, unknown>>;
  policies: Array<Record<string, unknown>>;
};

const FACTOR_DECISION_BAR_SECONDS = 5 * 60;

function eventTimestampMs(value: unknown): number {
  if (typeof value === "string" && value.trim()) {
    const numeric = Number(value);
    if (Number.isFinite(numeric) && numeric > 0) {
      return numeric > 1e12 ? numeric : numeric * 1000;
    }
    const parsed = Date.parse(value);
    if (!Number.isNaN(parsed)) return parsed;
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric <= 0) return 0;
  return numeric > 1e12 ? numeric : numeric * 1000;
}

function optionalMetricValue(item: Record<string, unknown>, keys: string[], digits: number): string {
  const value = pick(item, keys);
  if (value === undefined || value === null || value === "") return "未知";
  return formatDecimal(value, digits);
}

function uniqueLabels(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}

function admissionBlockerCodes(item: Record<string, unknown>): string[] {
  return uniqueLabels(pickArray(item, ["blockers"]).map((value) => String(value)));
}

function admissionBlockers(item: Record<string, unknown>): string[] {
  return admissionBlockerCodes(item).map((value) => translateReasonText(value));
}

function admissionOwner(item: Record<string, unknown>): string {
  const owner = pickString(item, ["admission_owner"], "");
  return owner ? translateDisplayValue(owner) : "开仓准入";
}

type AdmissionSummary = {
  owner: string;
  blockers: string[];
};

function summarizeAdmissionSkips(items: Array<Record<string, unknown>>): AdmissionSummary | null {
  if (!items.length) return null;
  const owners = uniqueLabels(
    items.flatMap((item) => admissionOwner(item).split(" + ")),
  );
  const blockerCodes = uniqueLabels(items.flatMap((item) => admissionBlockerCodes(item)));
  return {
    owner: owners.join(" + "),
    blockers: blockerCodes.map((value) => translateReasonText(value)),
  };
}

function policyExecutionStatus(item: Record<string, unknown>): { label: string; tone: ChainTone } {
  const executionCategory = pickString(item, ["execution_category"], "unknown");
  if (executionCategory === "applied") return { label: "执行已应用", tone: "ok" };
  if (executionCategory === "skipped") return { label: "执行跳过", tone: "warn" };
  if (executionCategory === "blocked") return { label: "执行拦截", tone: "bad" };
  if (executionCategory === "failed") return { label: "执行失败", tone: "bad" };
  return pickBoolean(item, ["allowed", "ok", "pass"], false)
    ? { label: "策略通过", tone: "mute" }
    : { label: "策略拦截", tone: "bad" };
}

function executionChainOutcome(
  policies: Array<Record<string, unknown>>,
  admissionSkips: Array<Record<string, unknown>>,
  factorSignals: Array<Record<string, unknown>>,
  currentPositionIds: readonly string[],
  currentPositionsKnown: boolean,
  policyPending: boolean,
  policyRequestFailed: boolean,
): { label: string; detail: string; tone: ChainTone } {
  const positionIdSet = new Set(currentPositionIds);
  const hasConfirmedPosition = policies.some((item) => {
    const positionId = pickString(item, ["position_id"], "");
    return Boolean(positionId) && positionIdSet.has(positionId);
  });
  if (hasConfirmedPosition) {
    return { label: "已开仓 · 持仓确认", detail: "当前持仓已匹配这条决策", tone: "ok" };
  }

  const hasApplied = policies.some((item) => (
    pickBoolean(item, ["execution_applied"], false)
    || pickString(item, ["execution_category"], "") === "applied"
  ));
  if (hasApplied) {
    return {
      label: currentPositionsKnown ? "已执行 · 未匹配当前持仓" : "已执行 · 待持仓确认",
      detail: currentPositionsKnown ? "请求已执行，当前持仓表未匹配该持仓 ID" : "请求已执行，等待持仓事实确认",
      tone: "ok",
    };
  }

  if (policies.some((item) => pickString(item, ["execution_category"], "") === "failed")) {
    return { label: "未开仓 · 执行失败", detail: "策略通过后执行阶段失败", tone: "bad" };
  }
  if (policies.some((item) => pickString(item, ["execution_category"], "") === "blocked")) {
    return { label: "未开仓 · 执行拦截", detail: "执行阶段被安全边界拦截", tone: "bad" };
  }
  if (policies.some((item) => pickString(item, ["execution_category"], "") === "skipped")) {
    return { label: "未开仓 · 未执行", detail: "策略记录存在，但没有发送开仓请求", tone: "warn" };
  }
  const hasDeniedPolicy = policies.some((item) => {
    const policyFlag = pick(item, ["allowed", "ok", "pass"]);
    return policyFlag !== undefined && policyFlag !== null
      && !pickBoolean(item, ["allowed", "ok", "pass"], true);
  });
  if (hasDeniedPolicy) {
    return { label: "未开仓 · 策略拦截", detail: "策略裁决未通过，未进入执行阶段", tone: "bad" };
  }
  if (policies.length) {
    return { label: "已裁决 · 执行未知", detail: "已有策略裁决，当前数据未包含执行结果", tone: "warn" };
  }
  const admission = summarizeAdmissionSkips(admissionSkips);
  if (admission) {
    return {
      label: "未开仓 · 前置拦截",
      detail: `${admission.blockers.length ? `原因：${admission.blockers.join(" + ")}` : "前置拦截"} · 风控裁决未调用 · 执行意图未创建`,
      tone: "bad",
    };
  }
  if (policyPending) {
    return { label: "策略读取中", detail: "策略接口尚未返回，本行暂不能确认是否开仓", tone: "warn" };
  }
  if (policyRequestFailed) {
    return { label: "策略接口异常", detail: "策略裁决接口读取失败，不等于后端停止", tone: "bad" };
  }

  const factorPassed = factorSignals.some((item) => {
    const gateReason = pickString(item, ["gate_reason", "gate_result.reason"], "");
    return pickBoolean(item, ["gate_passed"], false) || gateReason.startsWith("passed");
  });
  return factorPassed
    ? { label: "后续链路待确认", detail: "因子已通过，但未读到前置拦截或策略裁决记录", tone: "warn" }
    : { label: "未进入策略裁决", detail: "没有有效因子信号通过策略裁决入口", tone: "mute" };
}

export function RiskPanel({
  riskData,
  riskRequestFailed,
  embedded = false,
  factorSignals = [],
  factorSignalsPending = false,
  factorSignalsRequestFailed = false,
  currentPositionIds = [],
  currentPositionsKnown = false,
  positionsContent,
}: {
  riskData: unknown;
  riskRequestFailed: boolean;
  embedded?: boolean;
  factorSignals?: unknown[];
  factorSignalsPending?: boolean;
  factorSignalsRequestFailed?: boolean;
  currentPositionIds?: readonly string[];
  currentPositionsKnown?: boolean;
  positionsContent?: ReactNode;
}) {
  const dbQuery = useQuery({
    queryKey: queryKeys.dbHealth,
    queryFn: getSystemDbHealth,
    staleTime: 5_000,
  });
  const readinessQuery = useBackendReadinessQuery();
  const policyQuery = useQuery({
    queryKey: queryKeys.riskPolicyVerdicts,
    queryFn: () => getRiskPolicyVerdicts(50),
    staleTime: 5_000,
  });
  const tradeTracesQuery = useQuery({
    queryKey: queryKeys.riskTradeTraces,
    queryFn: () => getRecentTradeTraces(20),
    staleTime: 10_000,
  });

  const risk = asRecord(riskData);
  const canonicalRisk = decodeCanonicalRiskSnapshot(riskData);
  // The dedicated policy endpoint is the only source for this panel. Falling
  // back to risk.summary would bind one endpoint's data to another endpoint's
  // freshness and could turn an old nested projection green.
  const policy = asRecord(policyQuery.data);
  const systemHealth = asRecord(risk.system_health);
  const readiness = asRecord(readinessQuery.data);
  const readinessDimensions = asRecord(readiness.readiness_dimensions);
  const readinessBlockers = asRecord(readinessDimensions.blockers);
  const readinessRisk = asRecord(readiness.risk_metrics);
  const riskInputsFact = readFactComponent(riskData, "risk_inputs", "risk.inputs.v1");
  const systemHealthFact = readFactComponent(riskData, "system_health", "system.runtime-health.v1");
  const dbFact = readFact(dbQuery.data, "system.db-health.v2");
  const readinessFact = readFact(readinessQuery.data, "ops.backend-readiness.v2");
  const policyFact = readFact(policyQuery.data, "risk.policy-verdicts.v2");
  const traceFact = readFact(tradeTracesQuery.data, "risk.trade-trace-recent.v2");
  const dbRequestFailed = dbQuery.isError || dbQuery.isRefetchError;
  const readinessRequestFailed = readinessQuery.isError || readinessQuery.isRefetchError;
  const policyRequestFailed = policyQuery.isError || policyQuery.isRefetchError;
  const traceRequestFailed = tradeTracesQuery.isError || tradeTracesQuery.isRefetchError;
  const riskKnown = factIsKnown(riskInputsFact, riskRequestFailed) && canonicalRisk.contractKnown;
  const healthKnown = factIsKnown(systemHealthFact, riskRequestFailed);
  const dbKnown = factIsKnown(dbFact, dbRequestFailed);
  const readinessKnown = factIsKnown(readinessFact, readinessRequestFailed);
  const traceKnown = factIsKnown(traceFact, traceRequestFailed);
  const riskDisplayable = factHasDisplayValue(riskInputsFact, riskRequestFailed) && canonicalRisk.contractKnown;
  const healthDisplayable = factHasDisplayValue(systemHealthFact, riskRequestFailed);
  const dbDisplayable = factHasDisplayValue(dbFact, dbRequestFailed);
  const readinessDisplayable = factHasDisplayValue(readinessFact, readinessRequestFailed);
  const riskFactLabel = factStatusLabel(riskInputsFact);
  const riskMetricLabel = (status: string, displayable: boolean): string => {
    if (displayable) return status === "known" ? "暂无数值" : translateDisplayValue(status || "unknown");
    return riskFactLabel;
  };
  const riskFactDetail = riskInputsFact.state === "stale" && riskInputsFact.observed_at
    ? `最后观测 ${formatTime(riskInputsFact.observed_at)} · 仅展示，不用于开仓`
    : riskKnown
      ? "当前权威风险事实"
      : "等待权威风险输入";
  const riskFactTone = factBoundTone(riskInputsFact, riskKnown ? "mute" : "warn", riskRequestFailed);
  const varKnown = riskKnown && knownMetric(canonicalRisk.var95.status);
  const stressKnown = riskKnown && knownMetric(canonicalRisk.stress.status);
  const concentrationKnown = riskKnown && knownMetric(canonicalRisk.concentration.status);
  const varDisplayable = riskDisplayable && knownMetric(canonicalRisk.var95.status);
  const var99Displayable = riskDisplayable && knownMetric(canonicalRisk.var99.status);
  const kellyDisplayable = riskDisplayable && knownMetric(canonicalRisk.kelly.status);
  const stressDisplayable = riskDisplayable && knownMetric(canonicalRisk.stress.status);
  const concentrationDisplayable = riskDisplayable && knownMetric(canonicalRisk.concentration.status);

  const policyCounts = asRecord(policy.counts);
  const executionCounts = asRecord(policy.execution_counts);
  const allowed = pickNumber(policyCounts, ["allowed"], 0);
  const blocked = pickNumber(policyCounts, ["blocked"], 0);
  const executionApplied = pickNumber(executionCounts, ["applied"], 0);
  const executionSkipped = pickNumber(executionCounts, ["skipped"], 0);
  const totalVerdicts = allowed + blocked;
  const policyAllowedRate = totalVerdicts ? (allowed / totalVerdicts) * 100 : 0;
  const policyItems = useMemo(() => pickArray(policy, ["items", "recent_items", "decisions", "history"]), [policy]);
  const prePolicySkips = useMemo(() => pickArray(policy, ["pre_policy_skips"]), [policy]);
  const tradeTraces = useMemo(() => pickArray(tradeTracesQuery.data, ["items", "traces", "rows"]), [tradeTracesQuery.data]);
  const executionChainRows = useMemo<ExecutionChainRow[]>(() => {
    const grouped = new Map<string, ExecutionChainRow>();
    const add = (kind: "factor" | "admission" | "policy", raw: unknown, index: number) => {
      const item = asRecord(raw);
      const timeValue = kind === "factor"
        ? pick(item, ["ts", "time"])
        : pick(item, ["decision_ts", "time", "ts", "created_at"]);
      const timestamp = eventTimestampMs(timeValue);
      const key = timestamp > 0
        ? `time:${Math.round(timestamp / 1000)}`
        : `${kind}:${pickString(item, ["decision_id", "tick", "id"], String(index))}:${index}`;
      const existing = grouped.get(key);
      if (existing) {
        if (kind === "factor") existing.factorSignals.push(item);
        else if (kind === "admission") existing.admissionSkips.push(item);
        else existing.policies.push(item);
        return;
      }
      grouped.set(key, {
        key,
        sortTime: timestamp,
        timeValue,
        factorSignals: kind === "factor" ? [item] : [],
        admissionSkips: kind === "admission" ? [item] : [],
        policies: kind === "policy" ? [item] : [],
      });
    };

    factorSignals.forEach((item, index) => add("factor", item, index));
    prePolicySkips.forEach((item, index) => add("admission", item, index));
    policyItems.forEach((item, index) => add("policy", item, index));
    return Array.from(grouped.values()).sort((a, b) => b.sortTime - a.sortTime);
  }, [factorSignals, policyItems, prePolicySkips]);

  const riskHealth = pickString(systemHealth, ["overall"], "");
  const riskBlocked = pickBoolean(systemHealth, ["trading_blocked", "blocked"], false);
  const critical = pickArray(systemHealth, ["critical_components", "blocking_components"]);
  const degraded = pickArray(systemHealth, ["degraded_components"]);
  const impact = pickString(systemHealth, ["impact_summary", "status", "summary"], "");
  const frontendReadyReported = pickBoolean(readiness, ["ready_for_frontend"], false);
  const liveExecutionReadyReported = pickBoolean(readiness, ["ready_for_live_execution"], false);
  const schema = pickString(readiness, ["schema_version"], "");
  const blockers = pickArray(readinessBlockers, ["live_execution"]);
  const readinessRiskStatus = pickString(readinessRisk, ["status"], "");
  const readinessRiskVarStatus = pickString(readinessRisk, ["var_status"], "");
  const readinessRiskKnown = pickBoolean(readinessRisk, ["ok"], false)
    && readinessRiskStatus === "known"
    && readinessRiskVarStatus === "known";
  const readinessRiskDisplay = readinessRiskKnown
    ? translateDisplayValue("known")
    : translateDisplayValue(readinessRiskStatus || readinessRiskVarStatus || "unknown");

  const dbList = pickArray(dbQuery.data, ["databases", "database_list", "items"]);
  const dbCounts = dbList.reduce<{ stale: number; missing: number; errors: number }>((counts, item) => {
    const row = asRecord(item);
    const freshness = pickString(row, ["freshness", "status", "state"], "");
    const exists = pickBoolean(row, ["exists"], true);
    const hasIssues = pickArray(row, ["errors", "issues"]).length > 0;
    if (!exists || freshness === "missing") counts.missing += 1;
    else if (["stale", "old"].includes(freshness)) counts.stale += 1;
    if (hasIssues || freshness === "error") counts.errors += 1;
    return counts;
  }, { stale: 0, missing: 0, errors: 0 });
  const dbStaleCount = dbCounts.stale;
  const dbMissingCount = dbCounts.missing;
  const dbErrorCount = dbCounts.errors;
  const dbStatus = pickString(dbQuery.data, ["overall", "status"], "");
  const dbHealthDisplay = !dbDisplayable
    ? "未知"
    : dbErrorCount
      ? `${formatDecimal(dbErrorCount, 0)} 错误`
      : dbMissingCount
        ? `${formatDecimal(dbMissingCount, 0)} 缺失`
        : dbStaleCount
          ? `${formatDecimal(dbStaleCount, 0)} 过期`
          : translateDisplayValue(dbStatus);
  const dbHealthTone = dbErrorCount || dbMissingCount
    ? "bad"
    : dbStaleCount
      ? "warn"
      : toneFromStatus(dbStatus);
  const riskBlockers = [...critical, ...blockers];
  const latestPolicy = asRecord(policyItems[0]);
  const latestPrePolicySkip = asRecord(prePolicySkips[0]);
  const latestTrace = asRecord(tradeTraces[0]);
  const latestPolicyAllowed = policyItems.length
    ? pickBoolean(latestPolicy, ["allowed", "ok", "pass"], false)
    : prePolicySkips.length ? false : undefined;
  const latestPolicyReason = policyItems.length
    ? translateReasonText(pickString(latestPolicy, ["reason", "message"], ""))
    : prePolicySkips.length
      ? `${admissionOwner(latestPrePolicySkip)} · ${admissionBlockers(latestPrePolicySkip).join(" + ")}`
      : "";
  const latestEvidenceStatus = policyItems.length
    ? latestPolicyAllowed === undefined ? "暂无裁决" : latestPolicyAllowed ? "最近允许" : "最近拦截"
    : prePolicySkips.length ? "开仓前置拦截" : "暂无裁决";
  const latestTraceOutcome = tradeTraces.length ? translateDisplayValue(pickString(latestTrace, ["outcome_label"], "")) : "";
  const hasSystemQueryError = riskRequestFailed || dbRequestFailed || readinessRequestFailed;
  const systemFactsKnown = healthKnown && riskKnown && dbKnown && readinessKnown;

  return (
    <section className={embedded ? "risk-panel risk-dashboard risk-dashboard-embedded" : "dashboard risk-dashboard"}>
      {!embedded ? <div className="dashboard-header">
        <div>
          <div className="eyebrow">风控审计</div>
          <h1>风控审计</h1>
          <p>展示后端权威的前瞻风险分布、策略裁决和运行阻断，不在浏览器重算风险。</p>
        </div>
        <div className="header-status">
          <StatusPill status={riskDisplayable ? `风险 ${riskFactLabel}` : "风险状态未知"} tone={riskFactTone} fact={riskInputsFact} requestFailed={riskRequestFailed} />
          <StatusPill status={healthDisplayable ? (riskBlocked ? "交易阻断" : "健康面未阻断") : "交易许可未知"} tone={factBoundTone(systemHealthFact, riskBlocked ? "bad" : "ok", riskRequestFailed)} fact={systemHealthFact} requestFailed={riskRequestFailed} />
          <StatusPill status={readinessDisplayable ? (frontendReadyReported ? "前端读取就绪" : "前端读取受限") : "前端读取状态未知"} tone={factBoundTone(readinessFact, frontendReadyReported ? "ok" : "warn", readinessRequestFailed)} fact={readinessFact} requestFailed={readinessRequestFailed} />
        </div>
      </div> : null}

      {!embedded ? <div className="stat-grid">
        <StatTile icon={ShieldCheck} label="系统健康" value={healthDisplayable ? translateDisplayValue(riskHealth) : "未知"} detail={translateDisplayValue(impact)} tone={toneFromStatus(riskHealth)} fact={systemHealthFact} requestFailed={riskRequestFailed} />
        <StatTile icon={ShieldAlert} label="策略拦截" value={formatDecimal(blocked, 0)} detail={`政策允许 ${formatDecimal(allowed, 0)} · 许可率 ${formatDecimal(policyAllowedRate, 1)}%`} tone={blocked ? "warn" : "ok"} fact={policyFact} requestFailed={policyRequestFailed} />
        <StatTile icon={Gauge} label="前瞻 VaR 95%" value={varDisplayable ? metricValue(canonicalRisk.var95.varPct, 4, "%") : riskMetricLabel(canonicalRisk.var95.status, varDisplayable)} detail={varDisplayable ? `CVaR ${metricValue(canonicalRisk.var95.cvarPct, 4, "%")} · ${canonicalRisk.var95.timeframe}` : riskFactDetail} tone={riskKnown ? "mute" : "warn"} fact={riskInputsFact} requestFailed={riskRequestFailed} />
        <StatTile icon={AlertTriangle} label="阻断组件" value={formatDecimal(critical.length + blockers.length, 0)} detail={`退化 ${formatDecimal(degraded.length, 0)} · DB 过期 ${formatDecimal(dbStaleCount, 0)} · 错误 ${formatDecimal(dbErrorCount, 0)}`} tone={!systemFactsKnown ? "pending" : critical.length || blockers.length || dbErrorCount || dbMissingCount ? "bad" : degraded.length || dbStaleCount ? "warn" : "ok"} fact={readinessFact} requestFailed={readinessRequestFailed} />
      </div> : null}

      <div className="dashboard-grid">
        <MetricCard title="Canonical 风险快照" className="wide-panel risk-control-overview">
          <div className="risk-mini-grid">
            <RiskMiniMetric label="95% VaR / CVaR" value={varDisplayable ? `${metricValue(canonicalRisk.var95.varPct, 4, "%")} / ${metricValue(canonicalRisk.var95.cvarPct, 4, "%")}` : riskMetricLabel(canonicalRisk.var95.status, varDisplayable)} detail={varDisplayable ? `${metricValue(canonicalRisk.var95.varUsd, 2, " USD")} / ${metricValue(canonicalRisk.var95.cvarUsd, 2, " USD")}` : riskFactDetail} tone={riskFactTone} fact={riskInputsFact} requestFailed={riskRequestFailed} />
            <RiskMiniMetric label="99% 只读对照" value={var99Displayable ? `${metricValue(canonicalRisk.var99.varPct, 4, "%")} / ${metricValue(canonicalRisk.var99.cvarPct, 4, "%")}` : riskMetricLabel(canonicalRisk.var99.status, var99Displayable)} detail={var99Displayable ? "只做对照，不增加风险阈值" : riskFactDetail} tone={riskFactTone} fact={riskInputsFact} requestFailed={riskRequestFailed} />
            <RiskMiniMetric label="凯利仓位系数" value={kellyDisplayable ? metricValue(canonicalRisk.kelly.fraction, 4) : riskMetricLabel(canonicalRisk.kelly.status, kellyDisplayable)} detail={kellyDisplayable ? `样本 ${metricValue(canonicalRisk.kelly.closedTrades, 0)} · 胜率 ${metricValue(canonicalRisk.kelly.winRate === null ? null : canonicalRisk.kelly.winRate * 100, 1, "%")}` : riskFactDetail} tone={riskFactTone} fact={riskInputsFact} requestFailed={riskRequestFailed} />
            <RiskMiniMetric label="政策许可率（前端计算）" value={`${formatDecimal(policyAllowedRate, 1)}%`} detail={`政策允许 ${formatDecimal(allowed, 0)} / 政策拦截 ${formatDecimal(blocked, 0)}`} tone={factBoundTone(policyFact, blocked ? "warn" : "ok", policyRequestFailed)} fact={policyFact} requestFailed={policyRequestFailed} />
            <RiskMiniMetric label="组件异常" value={formatDecimal(riskBlockers.length + degraded.length, 0)} detail={`阻断 ${formatDecimal(riskBlockers.length, 0)} · 退化 ${formatDecimal(degraded.length, 0)}`} tone={!systemFactsKnown ? "pending" : riskBlockers.length ? "bad" : degraded.length ? "warn" : "ok"} fact={systemHealthFact} requestFailed={riskRequestFailed} />
            <RiskMiniMetric label="数据健康" value={dbHealthDisplay} detail={`库 ${dbDisplayable ? formatDecimal(dbList.length, 0) : "未知"} · 过期 ${dbDisplayable ? formatDecimal(dbStaleCount, 0) : "未知"} · 缺失 ${dbDisplayable ? formatDecimal(dbMissingCount, 0) : "未知"} · 错误 ${dbDisplayable ? formatDecimal(dbErrorCount, 0) : "未知"} · 合约 ${schema}${dbRequestFailed ? " · 接口异常" : ""}`} tone={factBoundTone(dbFact, dbHealthTone, dbRequestFailed)} fact={dbFact} requestFailed={dbRequestFailed} />
          </div>

          <div className="risk-control-grid">
            <section className="risk-control-section">
              <div className="risk-section-head">
                <h3>前瞻分布</h3>
                <StatusPill status={varKnown ? "已确认" : varDisplayable ? "已过期 · 仅展示" : riskMetricLabel(canonicalRisk.var95.status, varDisplayable)} tone={varKnown ? "ok" : riskFactTone} fact={riskInputsFact} requestFailed={riskRequestFailed} />
              </div>
              <div className="field-list risk-compact-fields">
                <Field label="周期" value={varDisplayable ? `${canonicalRisk.var95.horizon} · ${canonicalRisk.var95.timeframe}` : riskMetricLabel(canonicalRisk.var95.status, varDisplayable)} />
                <Field label="收益样本" value={varDisplayable ? metricValue(canonicalRisk.var95.sampleCount, 0) : riskMetricLabel(canonicalRisk.var95.status, varDisplayable)} />
                <Field label="当前权益" value={varDisplayable ? metricValue(canonicalRisk.var95.currentEquity, 2, " USD") : riskMetricLabel(canonicalRisk.var95.status, varDisplayable)} />
                <Field label="当前净名义敞口" value={varDisplayable ? metricValue(canonicalRisk.var95.currentNetNotionalUsd, 2, " USD") : riskMetricLabel(canonicalRisk.var95.status, varDisplayable)} />
                <Field label="数据窗口" value={varDisplayable ? `${formatTime(canonicalRisk.sourceWindowStart)} → ${formatTime(canonicalRisk.sourceWindowEnd)}` : riskMetricLabel(canonicalRisk.var95.status, varDisplayable)} />
                <Field label="输入指纹" value={riskDisplayable && canonicalRisk.inputFingerprint ? compactId(canonicalRisk.inputFingerprint) : riskFactLabel} />
              </div>
            </section>

            <section className="risk-control-section">
              <div className="risk-section-head">
                <h3>压力与集中</h3>
                <StatusPill status={stressKnown && concentrationKnown ? "已知" : "未确认"} tone={factBoundTone(riskInputsFact, stressKnown && concentrationKnown ? "ok" : "warn", riskRequestFailed)} fact={riskInputsFact} requestFailed={riskRequestFailed} />
              </div>
              <div className="field-list risk-compact-fields">
                <Field label="压力损失" value={stressDisplayable ? metricValue(canonicalRisk.stress.lossPct, 4, "%") : riskMetricLabel(canonicalRisk.stress.status, stressDisplayable)} />
                <Field label="压力损失金额" value={stressDisplayable ? metricValue(canonicalRisk.stress.lossUsd, 2, " USD") : riskMetricLabel(canonicalRisk.stress.status, stressDisplayable)} />
                <Field label="压力持仓数" value={stressDisplayable ? metricValue(canonicalRisk.stress.positionCount, 0) : riskMetricLabel(canonicalRisk.stress.status, stressDisplayable)} />
                <Field label="集中度" value={concentrationDisplayable ? metricValue(canonicalRisk.concentration.pct, 4, "%") : riskMetricLabel(canonicalRisk.concentration.status, concentrationDisplayable)} />
                <Field label="最大集中品种" value={concentrationDisplayable ? canonicalRisk.concentration.maxSingleName || "当前无持仓" : riskMetricLabel(canonicalRisk.concentration.status, concentrationDisplayable)} />
                <Field label="适用状态" value={concentrationDisplayable ? (canonicalRisk.concentration.applicable ? canonicalRisk.concentration.safe ? "适用且安全" : "适用且超限" : "空仓不适用") : riskMetricLabel(canonicalRisk.concentration.status, concentrationDisplayable)} />
              </div>
            </section>

            <section className="risk-control-section">
              <div className="risk-section-head">
                <h3>系统组件</h3>
                <StatusPill status={!riskDisplayable ? "未知" : hasSystemQueryError || riskBlockers.length ? "异常" : degraded.length ? "退化" : "正常"} tone={!systemFactsKnown && riskDisplayable ? "pending" : !systemFactsKnown ? "warn" : hasSystemQueryError || riskBlockers.length ? "bad" : degraded.length ? "warn" : "ok"} fact={systemHealthFact} requestFailed={riskRequestFailed} />
              </div>
              <div className="field-list risk-compact-fields">
                <Field label="前端读取就绪" value={readinessDisplayable ? (frontendReadyReported ? "是" : "否") : "未知"} tone={factBoundTone(readinessFact, frontendReadyReported ? "ok" : "warn", readinessRequestFailed)} />
                <Field label="交易执行就绪" value={readinessDisplayable ? (liveExecutionReadyReported ? "是" : "否") : "未知"} tone={factBoundTone(readinessFact, !readinessDisplayable ? "warn" : liveExecutionReadyReported ? "ok" : "bad", readinessRequestFailed)} />
                <Field label="权威风险契约" value={riskRequestFailed ? "异常" : riskDisplayable ? canonicalRisk.schemaVersion : "未知"} tone={riskRequestFailed ? "bad" : riskKnown ? "ok" : riskDisplayable ? "pending" : "warn"} />
                <Field label="Readiness 风险投影" value={readinessDisplayable ? readinessRiskDisplay : "未知"} tone={factBoundTone(readinessFact, readinessRiskKnown ? "ok" : "warn", readinessRequestFailed)} />
                <Field label="就绪接口" value={readinessRequestFailed ? "异常" : readinessDisplayable ? "有事实" : "未知"} tone={readinessRequestFailed ? "bad" : readinessKnown ? "ok" : readinessDisplayable ? "pending" : "warn"} />
                <Field label="数据库状态" value={dbDisplayable ? dbStatus : "未知"} tone={factBoundTone(dbFact, dbHealthTone, dbRequestFailed)} />
                <Field label="数据库总数" value={dbDisplayable ? formatDecimal(dbList.length, 0) : "未知"} />
                <Field label="数据库过期" value={dbDisplayable ? formatDecimal(dbStaleCount, 0) : "未知"} tone={dbStaleCount ? "warn" : dbDisplayable ? "ok" : "pending"} />
                <Field label="数据库缺失" value={dbDisplayable ? formatDecimal(dbMissingCount, 0) : "未知"} tone={dbMissingCount ? "bad" : dbDisplayable ? "ok" : "pending"} />
                <Field label="数据库错误" value={dbDisplayable ? formatDecimal(dbErrorCount, 0) : "未知"} tone={dbErrorCount ? "bad" : dbDisplayable ? "ok" : "pending"} />
              </div>
              <div className="compact-list risk-inline-badges">
                {riskRequestFailed ? <span className="data-badge data-badge-bad">risk-summary 异常</span> : null}
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
                <StatusPill status={latestEvidenceStatus} tone={factBoundTone(policyFact, latestPolicyAllowed === undefined ? "mute" : latestPolicyAllowed ? "ok" : "bad", policyRequestFailed)} />
              </div>
              <div className="field-list risk-compact-fields">
                <Field label="最近原因" value={latestPolicyReason} />
                <Field label="证据链结果" value={latestTraceOutcome} />
                <Field label="裁决记录" value={formatDecimal(policyItems.length, 0)} />
                <Field label="前置拦截" value={formatDecimal(prePolicySkips.length, 0)} tone={prePolicySkips.length ? "bad" : "mute"} />
                <Field label="证据链" value={formatDecimal(tradeTraces.length, 0)} />
              </div>
            </section>
          </div>
        </MetricCard>

        <MetricCard title="开仓决策链" className="wide-panel risk-audit-card execution-chain-card">
          <div className="execution-chain-summary">
            <div className="policy-summary-strip">
              <span><b>{formatDecimal(allowed, 0)}</b> 政策允许</span>
              <span className={blocked ? "policy-summary-bad" : ""}><b>{formatDecimal(blocked, 0)}</b> 政策拦截</span>
              <span><b>{formatDecimal(executionApplied, 0)}</b> 真实执行</span>
              <span><b>{formatDecimal(executionSkipped, 0)}</b> 未执行</span>
              <span><b>{formatDecimal(factorSignals.length, 0)}</b> 因子信号</span>
              <span className={prePolicySkips.length ? "policy-summary-bad" : ""}><b>{formatDecimal(prePolicySkips.length, 0)}</b> 前置拦截</span>
              <span><b>{formatDecimal(executionChainRows.length, 0)}</b> 合并链路</span>
            </div>
            <span className="execution-chain-summary-note">
              {policyRequestFailed || factorSignalsRequestFailed
                ? "接口异常已单独标记 · 不代表后端停止"
                : policyQuery.isPending
                  ? "策略接口读取中 · 不代表后端停止"
                  : "前置拦截与风控裁决分开显示 · 按时间合并重复记录 · 滚动查看完整链路"}
            </span>
          </div>

          <div className="execution-chain-log" role="log" aria-label="因子信号到开仓执行的决策链">
            {!executionChainRows.length && (policyQuery.isPending || factorSignalsPending) ? (
              <div className="empty-state-small">正在读取因子信号与策略裁决…</div>
            ) : !executionChainRows.length ? (
              <div className="empty-state-small">{policyRequestFailed || factorSignalsRequestFailed ? "决策链读取异常，暂无可用缓存" : "暂无因子信号或策略裁决"}</div>
            ) : (
              executionChainRows.map((row) => {
                const outcome = executionChainOutcome(
                  row.policies,
                  row.admissionSkips,
                  row.factorSignals,
                  currentPositionIds,
                  currentPositionsKnown,
                  policyQuery.isPending,
                  policyRequestFailed,
                );
                const factorSummary = row.factorSignals.length
                  ? uniqueLabels(row.factorSignals.map((item) => {
                    const gateReason = translateReasonText(pickString(item, ["gate_reason", "gate_result.reason"], ""));
                    const gateLabel = gateReason || (pickBoolean(item, ["gate_passed"], false) ? "passed" : "未知");
                    return `Tick ${optionalMetricValue(item, ["tick"], 0)} · 战术 ${optionalMetricValue(item, ["tactical_score", "signal.tactical_score"], 4)} · 宏观 ${optionalMetricValue(item, ["macro_score", "signal.macro_score"], 4)} · 贡献 ${optionalMetricValue(item, ["n_contributing", "n_contributing_factors", "signal.n_contributing"], 0)} · 弃权 ${optionalMetricValue(item, ["n_abstain", "signal.n_abstain"], 0)} · 闸门 ${gateLabel}`;
                  })).join("；")
                  : "无因子快照";
                const admission = summarizeAdmissionSkips(row.admissionSkips);
                const policySummary = row.policies.length
                  ? uniqueLabels(row.policies.map((item) => {
                  const status = policyExecutionStatus(item);
                  const decisionId = pickString(item, ["decision_id", "id"], "");
                  const action = translateDisplayValue(pickString(item, ["action", "type"], "policy"));
                  const direction = translateDisplayValue(formatDirection(pick(item, ["direction", "side"])));
                  const title = [action, direction].filter(Boolean).join(" · ") || "策略裁决";
                    return `${title} · ${status.label}${decisionId ? ` · ${compactId(decisionId)}` : ""}`;
                  })).join("；")
                  : admission
                    ? `${admission.owner}拦截`
                    : policyQuery.isPending
                    ? "读取中"
                  : policyRequestFailed
                      ? "接口异常"
                      : "未生成策略裁决记录";
                const reasons = uniqueLabels([
                  ...(admission?.blockers || []),
                  ...row.policies.flatMap((item) => [
                    translateReasonText(pickString(item, ["reason", "message"], "")),
                    translateReasonText(pickString(item, ["execution_reason"], "")),
                  ]),
                ]);
                const symbols = uniqueLabels(row.policies.map((item) => pickString(item, ["symbol"], "")));
                const positionIds = uniqueLabels(row.policies.map((item) => compactId(pickString(item, ["position_id"], ""))));
                const recordSummary = [
                  symbols.join(" · "),
                  `因子 ${row.factorSignals.length}`,
                  admission ? "前置拦截" : "",
                  `策略 ${row.policies.length}`,
                ].filter(Boolean).join(" · ");
                const resultSummary = [
                  outcome.detail,
                  reasons.length && !row.admissionSkips.length ? `原因：${reasons.join(" · ")}` : "",
                ].filter(Boolean).join(" · ");
                const isFactorDecision = row.factorSignals.length > 0;
                const decisionWindow = isFactorDecision
                  ? formatTimeRange(row.timeValue, FACTOR_DECISION_BAR_SECONDS)
                  : "";

                return (
                  <article className={`execution-chain-item execution-chain-item-${outcome.tone}`} key={row.key}>
                    <div className="execution-chain-line">
                      <div className="execution-chain-time">
                        <strong>{decisionWindow || formatTime(row.timeValue) || "时间未知"}</strong>
                        <span>{decisionWindow ? "M5 决策K线 · 已收盘" : recordSummary}</span>
                      </div>
                      <div className="execution-chain-status">
                        <StatusPill status={outcome.label} tone={factBoundTone(policyFact, outcome.tone, Boolean(row.policies.length && policyRequestFailed))} />
                      </div>
                      <div className="execution-chain-path">
                        <span className="execution-chain-segment execution-chain-factor-segment"><b>因子</b> {factorSummary}</span>
                        <span className="execution-chain-separator" aria-hidden="true">→</span>
                        <span className="execution-chain-segment execution-chain-policy-segment"><b>策略</b> {policySummary}</span>
                        <span className="execution-chain-separator" aria-hidden="true">→</span>
                        <span className={`execution-chain-segment execution-chain-result-segment execution-chain-result-${outcome.tone}`}><b>结果</b> {resultSummary}</span>
                      </div>
                    </div>

                    {positionIds.length ? <div className="execution-chain-meta">持仓 ID：{positionIds.join(" · ")}</div> : null}
                  </article>
                );
              })
            )}
          </div>
          {policyRequestFailed || factorSignalsRequestFailed ? (
            <ul className="error-list">
              {policyQuery.isError ? <li>policy-verdicts：{policyQuery.error instanceof Error ? policyQuery.error.message : "请求失败"}</li> : null}
              {factorSignalsRequestFailed ? <li>factor-v4-recent-ticks：请求失败或数据未确认</li> : null}
            </ul>
          ) : null}
        </MetricCard>

        <div className="risk-audit-columns">
          <div className="risk-audit-column">
            {positionsContent}
          </div>

          <div className="risk-audit-column risk-trace-column">
            <MetricCard title="交易证据链" className="risk-audit-card risk-trace-card">
          {tradeTracesQuery.isPending && !tradeTracesQuery.data ? (
            <div className="empty-state-small">正在读取交易证据链…</div>
          ) : !tradeTraces.length ? (
            <div className="empty-state-small">{traceRequestFailed ? "交易证据链读取异常，暂无可用缓存" : "暂无交易证据链"}</div>
          ) : (
            <div className="trace-list">
              {tradeTraces.slice(0, 6).map((raw, index) => {
                const item = asRecord(raw);
                const rawSummary = pickString(item, ["summary_text"], "");
                const outcome = translateDisplayValue(pickString(item, ["outcome_label"], ""));
                const closeReason = translateDisplayValue(pickString(item, ["close_reason"], ""));
                const responsibility = translateDisplayValue(pickString(item, ["primary_responsibility"], ""));
                const pnl = extractTraceToken(rawSummary, "pnl");
                const largestContributionFactor = (
                  pickString(item, ["largest_contribution_factor", "primary_factor"], "")
                  || extractTraceToken(rawSummary, "largest_contribution_factor")
                  || extractTraceToken(rawSummary, "primary_factor")
                );
                const worstFactor = (
                  pickString(item, ["worst_factor"], "")
                  || extractTraceToken(rawSummary, "worst_factor")
                );
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
                      {largestContributionFactor ? <span className="trace-chip">最大贡献 {translateReasonText(largestContributionFactor)}</span> : null}
                      {worstFactor ? <span className="trace-chip">弱项 {translateReasonText(worstFactor)}</span> : null}
                    </div>
                  </article>
                );
              })}
            </div>
          )}
          {policyRequestFailed || traceRequestFailed ? (
            <ul className="error-list">
              {policyQuery.isError ? <li>policy-verdicts：{policyQuery.error instanceof Error ? policyQuery.error.message : "请求失败"}</li> : null}
              {tradeTracesQuery.isError ? <li>trade-trace：{tradeTracesQuery.error instanceof Error ? tradeTracesQuery.error.message : "请求失败"}</li> : null}
            </ul>
          ) : null}
            </MetricCard>
          </div>
        </div>
      </div>
    </section>
  );
}
