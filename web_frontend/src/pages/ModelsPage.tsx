import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { MetricCard } from "@/components/Card";
import { CompactMetric as ModelMiniMetric, Field, numberTone, toneFromStatus, type Tone } from "@/components/DashboardBits";
import { QueryErrorList } from "@/components/QueryErrorList";
import { StatusPill } from "@/components/StatusPill";
import {
  getFactorGovernanceLightgbmAdvisories,
  getFactorGovernanceLightgbmAudits,
  getLearningDatasetQualityHealth,
  getLearningDatasetReadiness,
  getMetaLightgbmAudits,
  getMetaLightgbmShadowReport,
  getMetaModelAdvisories,
  getModelCanaryReviews,
  getModelInferenceAudits,
  getModelPermissionAudits,
  getModelShadowQueue,
  getOffmarketHighLoadAudits,
  getOpenQualityLightgbmAudits,
  getPositionQualityLightgbmAudits,
  getHistoricalBacktestJob,
  startHistoricalBacktest,
} from "@/api/client";
import { asRecord, pick, pickArray, pickBoolean, pickNumber, pickRecord, pickString } from "@/lib/compat";
import { translateDisplayValue } from "@/lib/display";
import { formatDecimal, formatTime } from "@/lib/format";
import { useBackendReadinessQuery } from "@/hooks/useCoreQueries";
import { factBoundTone, factIsKnown, readFact } from "@/api/fact";

function formatPct(value: number): string {
  return `${formatDecimal(value * 100, 1)}%`;
}

function shortText(value: unknown, fallback = "", maxLength = 96): string {
  const text = String(value || "").trim();
  if (!text) return fallback;
  return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text;
}

function countItems(payload: unknown): number {
  return pickNumber(payload, ["count"], pickArray(payload, ["items"]).length);
}

function statusTone(status: string): Tone {
  const normalized = status.toLowerCase();
  if (["passed", "approved", "eligible", "shadow_candidate", "ok", "ready", "completed", "success"].includes(normalized)) {
    return "ok";
  }
  if ([
    "queued",
    "running",
    "pending",
    "review",
    "canary_ready",
    "shadow",
    "advisory",
    "blocked_by_governance",
    "blocked_by_baseline",
    "model_shadow_candidate",
    "rule_sidecar_candidate",
  ].includes(normalized)) {
    return "warn";
  }
  if (["failed", "rejected", "blocked", "error", "missing", "denied"].includes(normalized)) {
    return "bad";
  }
  return toneFromStatus(status);
}

function keyFor(item: Record<string, unknown>, index: number, keys: string[]): string {
  return `${pickString(item, keys, String(index))}-${index}`;
}

function objectSummary(value: unknown): string {
  if (!value) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return shortText(JSON.stringify(value), "", 140);
  } catch {
    return "";
  }
}

function distributionSummary(record: unknown): string {
  const dist = asRecord(record);
  const entries = Object.entries(dist)
    .filter(([, value]) => Number(value) > 0)
    .map(([key, value]) => `${translateDisplayValue(key)} ${formatDecimal(Number(value), 0)}`);
  return entries.length ? entries.join(" · ") : "";
}

function backtestReasonLabel(value: unknown): string {
  const reason = String(value || "");
  const labels: Record<string, string> = {
    no_closed_independent_trades: "区间内没有已平仓的独立交易",
    point_in_time_data_unverified: "历史数据的时间边界未通过检查",
    closed_bar_contract_failed: "包含未闭合K线",
    next_bar_execution_contract_failed: "下一根K线成交检查未通过",
    native_bid_ask_missing: "历史买卖价不完整",
    current_bounded_factor_generation_unverified: "因子版本不是当前受控版本",
    code_changed_during_replay: "运行期间代码发生变化",
    data_files_changed_during_replay: "运行期间历史数据发生变化",
  };
  return labels[reason] || translateDisplayValue(reason);
}

function localInputToIso(value: string): string | undefined {
  if (!value) return undefined;
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? undefined : timestamp.toISOString();
}

const MODEL_DEFINITIONS = [
  {
    type: "meta_model_lightgbm",
    name: "综合判断模型",
    purpose: "综合交易与复盘证据，判断整体策略应保持、收紧还是恢复。",
    output: "策略姿态建议",
  },
  {
    type: "open_quality_lightgbm",
    name: "开仓质量模型",
    purpose: "在开仓前评估当前机会质量，识别应放行或回避的入场。",
    output: "开仓质量评分",
  },
  {
    type: "position_quality_lightgbm",
    name: "持仓质量模型",
    purpose: "持仓期间评估继续持有、减仓或退出的质量信号。",
    output: "持仓质量评分",
  },
  {
    type: "factor_governance_lightgbm",
    name: "因子治理模型",
    purpose: "评估因子近期表现，生成弱化、保留或复核建议。",
    output: "因子治理建议",
  },
] as const;

const GATE_CHECK_LABELS: Record<string, string> = {
  artifact_fresh: "模型文件过期",
  auc: "区分能力不足",
  balanced_accuracy: "平衡准确率不足",
  distinct_positions: "独立持仓样本不足",
  distinct_trades: "独立交易样本不足",
  feature_schema: "特征版本不符合要求",
  generalization_gap: "训练与验证差距过大",
  governance_ready: "治理基线未通过",
  holdout_count: "验证样本不足",
  holdout_positions: "验证持仓不足",
  holdout_trades: "验证交易不足",
  majority_lift: "未超过简单基线",
  rule_lift: "未超过现有规则",
  sample_count: "训练样本不足",
};

function latestAuditTime(items: unknown[]): number {
  return items.reduce<number>((latest, raw) => {
    const item = asRecord(raw);
    return Math.max(
      latest,
      pickNumber(item, ["created_at", "event_ts", "updated_at", "finished_at"], 0),
    );
  }, 0);
}

function failedGateSummary(gate: Record<string, unknown>): string {
  const failed = pickArray(gate, ["failed_checks"])
    .map((item) => GATE_CHECK_LABELS[String(item)] || translateDisplayValue(String(item)))
    .filter(Boolean);
  return failed.length ? failed.slice(0, 3).join("、") : "";
}

function ModelEventItem({
  title,
  meta,
  status,
  tone,
  detail,
  score,
}: {
  title: string;
  meta: string;
  status?: string;
  tone?: Tone;
  detail?: string;
  score?: string;
}) {
  return (
    <div className="model-event-item">
      <div className="model-event-main">
        <div>
          <strong>{title}</strong>
          <span>{meta}</span>
        </div>
        <div className="model-event-side">
          {score ? <b>{score}</b> : null}
          {status ? <StatusPill status={status} tone={tone || statusTone(status)} /> : null}
        </div>
      </div>
      {detail ? <p>{detail}</p> : null}
    </div>
  );
}

export function ModelsPage({ embedded = false }: { embedded?: boolean }) {
  const [backtestJobId, setBacktestJobId] = useState("");
  const [backtestForm, setBacktestForm] = useState({
    start: "",
    end: "",
    maxBars: 5000,
  });
  const backtestMutation = useMutation({
    mutationFn: () => startHistoricalBacktest({
      symbol: "XAUUSD+",
      timeframe: "M5",
      start: localInputToIso(backtestForm.start),
      end: localInputToIso(backtestForm.end),
      max_bars: backtestForm.maxBars,
      warmup_bars: 150,
      initial_equity: 10_000,
      volume_lots: 0.01,
      commission_per_lot_round_turn: 18,
      slippage_price_each_fill: 0.035,
    }),
    onSuccess: (payload) => setBacktestJobId(pickString(payload, ["job_id"], "")),
  });
  const backtestJobQuery = useQuery({
    queryKey: ["historical-backtest", backtestJobId],
    queryFn: () => getHistoricalBacktestJob(backtestJobId),
    enabled: Boolean(backtestJobId),
    refetchInterval: (query) => {
      const status = pickString(query.state.data, ["status"], "");
      return ["done", "error", "cancelled"].includes(status) ? false : 5000;
    },
    retry: false,
  });
  const readinessQuery = useBackendReadinessQuery(60_000);
  const datasetQuery = useQuery({
    queryKey: ["learning-dataset-readiness"],
    queryFn: getLearningDatasetReadiness,
    staleTime: 20_000,
  });
  const metaReportQuery = useQuery({
    queryKey: ["models-meta-lightgbm-report"],
    queryFn: () => getMetaLightgbmShadowReport(200),
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const metaAuditsQuery = useQuery({
    queryKey: ["models-meta-lightgbm-audits"],
    queryFn: () => getMetaLightgbmAudits(20),
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const positionAuditsQuery = useQuery({
    queryKey: ["models-position-quality-audits"],
    queryFn: () => getPositionQualityLightgbmAudits(20),
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const openAuditsQuery = useQuery({
    queryKey: ["models-open-quality-audits"],
    queryFn: () => getOpenQualityLightgbmAudits(20),
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const factorAuditsQuery = useQuery({
    queryKey: ["models-factor-governance-audits"],
    queryFn: () => getFactorGovernanceLightgbmAudits(20),
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const factorAdvisoriesQuery = useQuery({
    queryKey: ["models-factor-governance-advisories"],
    queryFn: () => getFactorGovernanceLightgbmAdvisories(20),
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const shadowQueueQuery = useQuery({
    queryKey: ["models-shadow-queue"],
    queryFn: () => getModelShadowQueue(20),
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const canaryQuery = useQuery({
    queryKey: ["models-canary-reviews"],
    queryFn: () => getModelCanaryReviews(20),
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const inferenceQuery = useQuery({
    queryKey: ["models-inference-audits"],
    queryFn: () => getModelInferenceAudits(20),
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const permissionsQuery = useQuery({
    queryKey: ["models-permission-audits"],
    queryFn: () => getModelPermissionAudits(20),
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const metaAdvisoriesQuery = useQuery({
    queryKey: ["models-meta-advisories"],
    queryFn: () => getMetaModelAdvisories(20),
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const highLoadAuditsQuery = useQuery({
    queryKey: ["models-offmarket-high-load"],
    queryFn: () => getOffmarketHighLoadAudits(20),
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const qualityHealthQuery = useQuery({
    queryKey: ["models-quality-health"],
    queryFn: () => getLearningDatasetQualityHealth(1000),
    refetchInterval: 60_000,
    staleTime: 20_000,
  });

  const readiness = asRecord(readinessQuery.data);
  const readinessFact = readFact(readinessQuery.data, "ops.backend-readiness.v2");
  const readinessRequestFailed = readinessQuery.isError || readinessQuery.isRefetchError;
  const readinessKnown = factIsKnown(readinessFact, readinessRequestFailed);
  const models = asRecord(pick(readiness, ["models"]));
  const metaLightgbm = asRecord(pick(models, ["meta_lightgbm"]));
  const promotionGate = asRecord(pick(metaLightgbm, ["promotion_gate"]));
  const modelInfluence = asRecord(pick(models, ["influence"]));
  const modelInfluencePolicies = asRecord(pick(modelInfluence, ["models"]));
  const promotionGates = asRecord(pick(models, ["promotion_gates"]));
  const modelInfluenceEnabled = pickBoolean(modelInfluence, ["demo_enabled"], false);
  const highLoad = asRecord(pick(readiness, ["high_load"]));
  const dataset = asRecord(datasetQuery.data);
  const metaReport = asRecord(metaReportQuery.data);
  const artifactSummary = asRecord(pick(metaReport, ["artifact_summary"]));
  const artifactMetrics = asRecord(pick(artifactSummary, ["metrics"]));
  const holdoutMetrics = asRecord(pick(artifactMetrics, ["holdout"]));
  const governanceReadiness = asRecord(pick(artifactMetrics, ["governance_readiness"]));
  const ruleComparison = asRecord(pick(metaReport, ["rule_comparison"]));
  const capabilities = asRecord(pick(metaReport, ["capabilities"]));

  const metaAudits = pickArray(metaAuditsQuery.data, ["items"]);
  const positionAudits = pickArray(positionAuditsQuery.data, ["items"]);
  const openAudits = pickArray(openAuditsQuery.data, ["items"]);
  const factorAudits = pickArray(factorAuditsQuery.data, ["items"]);
  const shadowQueue = pickArray(shadowQueueQuery.data, ["items"]);
  const canaryReviews = pickArray(canaryQuery.data, ["items"]);
  const inferenceAudits = pickArray(inferenceQuery.data, ["items"]);
  const permissionAudits = pickArray(permissionsQuery.data, ["items"]);
  const metaAdvisories = pickArray(metaAdvisoriesQuery.data, ["items"]);
  const topFeatures = pickArray(artifactSummary, ["top_features"]);
  const qualityHealth = asRecord(qualityHealthQuery.data);
  const entryContext = asRecord(pick(qualityHealth, ["entry_context"]));
  const entryCoverageRatio = asRecord(pick(entryContext, ["coverage_ratio"]));
  const evidenceHealth = asRecord(pick(qualityHealth, ["evidence_contract"]));
  const evidenceCounts = asRecord(pick(evidenceHealth, ["counts"]));

  const evaluatedCount = pickNumber(metaReport, ["evaluated_count"], pickNumber(metaLightgbm, ["report.evaluated_count"], 0));
  const auditCount = pickNumber(metaReport, ["audit_count"], countItems(metaAuditsQuery.data));
  const accuracy = pickNumber(metaReport, ["accuracy"], pickNumber(metaLightgbm, ["report.accuracy"], 0));
  const holdoutAccuracy = pickNumber(holdoutMetrics, ["accuracy"], 0);
  const holdoutRuleAccuracy = pickNumber(holdoutMetrics, ["rule_accuracy"], 0);
  const holdoutMajorityAccuracy = pickNumber(holdoutMetrics, ["majority_baseline_accuracy"], 0);
  const ruleLiftVsMajority = pickNumber(holdoutMetrics, ["rule_lift_vs_majority"], 0);
  const governanceReadinessStatus = pickString(governanceReadiness, ["status"], "");
  const recommendedSource = pickString(governanceReadiness, ["recommended_source"], "");
  const degradationReason = pickString(governanceReadiness, ["degradation_reason"], "");
  const ruleAgreement = pickNumber(ruleComparison, ["agreement_rate"], 0);
  const modelReady = pickNumber(dataset, ["model_ready", "ready", "quality.model_ready"], 0);
  const needsAttention = pickNumber(dataset, ["needs_attention", "not_ready", "quality.needs_attention"], 0);
  const sampleTotal = pickNumber(dataset, ["total", "count", "sample_count"], modelReady + needsAttention);
  const entryOpenDecisions = pickNumber(entryContext, ["open_decisions"], 0);
  const entryContextStatus = pickString(entryContext, ["status"], "warming");
  const entryBarCoverage = pickNumber(entryCoverageRatio, ["bar_context"], 0);
  const entryExecutionCoverage = pickNumber(entryCoverageRatio, ["execution_context"], 0);
  const entryMicroCoverage = pickNumber(entryCoverageRatio, ["market_micro_context"], 0);
  const evidenceBadTotal = pickNumber(evidenceCounts, ["bad_total"], 0);
  const gateEligible = pickBoolean(promotionGate, ["eligible_for_live", "eligible_for_governor_review", "ok"], false);
  const gateDecision = pickString(promotionGate, ["decision", "status"], gateEligible ? "eligible" : "shadow_only");
  const highLoadProfile = pickString(highLoad, ["profile", "status"], "");
  const advisoryOnly = pickBoolean(capabilities, ["advisory_only"], true);

  const auditsByModel: Record<string, unknown[]> = {
    meta_model_lightgbm: metaAudits,
    open_quality_lightgbm: openAudits,
    position_quality_lightgbm: positionAudits,
    factor_governance_lightgbm: factorAudits,
  };
  const modelRows = MODEL_DEFINITIONS.map((definition) => {
    const policy = asRecord(pick(modelInfluencePolicies, [definition.type]));
    const gate = asRecord(pick(promotionGates, [definition.type]));
    const audits = auditsByModel[definition.type] || [];
    const stage = pickString(policy, ["stage"], "shadow");
    const allowedEffects = pickArray(policy, ["allowed_effects"]).map(String).filter(Boolean);
    const applied = pickNumber(policy, ["applied"], 0);
    const lastDecisionAt = pickNumber(policy, ["last_decision_at"], 0);
    const latestOutputAt = Math.max(lastDecisionAt, latestAuditTime(audits));
    const gatePassed = pickBoolean(gate, ["passed"], false);
    const gateMetrics = pickRecord(gate, ["metrics"]);
    const trainingSources = pickRecord(gateMetrics, ["training_sources"]);
    const comparison = pickRecord(gateMetrics, ["augmentation_comparison"]);
    const baselineHoldout = pickRecord(comparison, ["baseline_real_holdout"]);
    const augmentedHoldout = pickRecord(comparison, ["augmented_real_holdout"]);
    const baselineChange = (
      pickNumber(augmentedHoldout, ["balanced_accuracy"], 0)
      - pickNumber(baselineHoldout, ["balanced_accuracy"], 0)
    );
    const sourceSummary = Object.keys(trainingSources || {}).length
      ? `真实 ${formatDecimal(pickNumber(trainingSources, ["real_train_samples"], 0), 0)} · 回测 ${formatDecimal(pickNumber(trainingSources, ["historical_replay_samples"], 0), 0)} · 真实验证 ${formatDecimal(pickNumber(gateMetrics, ["real_holdout_count"], 0), 0)} · 相对基线 ${baselineChange >= 0 ? "+" : ""}${formatPct(baselineChange)}`
      : "";
    const affectsTrading = modelInfluenceEnabled && stage !== "shadow" && allowedEffects.length > 0;
    const reasons = [
      !modelInfluenceEnabled ? "模型影响功能未启用" : "",
      stage === "shadow" ? "当前仅做影子观察" : "",
      !allowedEffects.length ? "未授权影响交易" : "",
      !gatePassed ? failedGateSummary(gate) || "模型准入检查未通过" : "",
    ].filter(Boolean);
    return {
      ...definition,
      affectsTrading,
      participation: affectsTrading ? "已接入受控决策" : "未参与交易决策",
      participationTone: (affectsTrading ? "ok" : "warn") as Tone,
      permission: allowedEffects.length
        ? allowedEffects.map(translateDisplayValue).join("、")
        : `${definition.output}，不下单、不改风控`,
      reason: reasons.join("；"),
      latestOutputAt,
      auditCount: audits.length,
      applied,
      sourceSummary,
    };
  });
  const participatingModelCount = modelRows.filter((model) => model.affectsTrading).length;
  const observedModelCount = modelRows.filter((model) => model.latestOutputAt > 0).length;

  const modelQueries = [
    readinessQuery,
    datasetQuery,
    metaReportQuery,
    metaAuditsQuery,
    positionAuditsQuery,
    openAuditsQuery,
    factorAuditsQuery,
    factorAdvisoriesQuery,
    shadowQueueQuery,
    canaryQuery,
    inferenceQuery,
    permissionsQuery,
    metaAdvisoriesQuery,
    highLoadAuditsQuery,
    qualityHealthQuery,
  ];
  const hasError = modelQueries.some((query) => query.isError || query.isRefetchError);
  const isRefreshing = modelQueries.some((query) => query.isFetching);
  const modelFactsKnown = readinessKnown && [
    factIsKnown(readFact(datasetQuery.data, "learning.dataset-readiness.v2"), datasetQuery.isError || datasetQuery.isRefetchError),
    factIsKnown(readFact(metaReportQuery.data, "learning.model-meta-lightgbm-shadow-report.v2"), metaReportQuery.isError || metaReportQuery.isRefetchError),
    factIsKnown(readFact(metaAuditsQuery.data, "learning.model-meta-lightgbm-audits.v2"), metaAuditsQuery.isError || metaAuditsQuery.isRefetchError),
    factIsKnown(readFact(positionAuditsQuery.data, "learning.model-position-quality-audits.v2"), positionAuditsQuery.isError || positionAuditsQuery.isRefetchError),
    factIsKnown(readFact(openAuditsQuery.data, "learning.model-open-quality-audits.v2"), openAuditsQuery.isError || openAuditsQuery.isRefetchError),
    factIsKnown(readFact(factorAuditsQuery.data, "learning.factor-governance-lightgbm-audits.v2"), factorAuditsQuery.isError || factorAuditsQuery.isRefetchError),
    factIsKnown(readFact(factorAdvisoriesQuery.data, "learning.factor-governance-lightgbm-advisories.v2"), factorAdvisoriesQuery.isError || factorAdvisoriesQuery.isRefetchError),
    factIsKnown(readFact(shadowQueueQuery.data, "learning.model-shadow-queue.v2"), shadowQueueQuery.isError || shadowQueueQuery.isRefetchError),
    factIsKnown(readFact(canaryQuery.data, "learning.model-canary-reviews.v2"), canaryQuery.isError || canaryQuery.isRefetchError),
    factIsKnown(readFact(inferenceQuery.data, "learning.model-inference-audits.v2"), inferenceQuery.isError || inferenceQuery.isRefetchError),
    factIsKnown(readFact(permissionsQuery.data, "learning.model-permission-audits.v2"), permissionsQuery.isError || permissionsQuery.isRefetchError),
    factIsKnown(readFact(metaAdvisoriesQuery.data, "learning.model-meta-advisories.v2"), metaAdvisoriesQuery.isError || metaAdvisoriesQuery.isRefetchError),
    factIsKnown(readFact(highLoadAuditsQuery.data, "learning.model-offmarket-high-load-audits.v2"), highLoadAuditsQuery.isError || highLoadAuditsQuery.isRefetchError),
    factIsKnown(readFact(qualityHealthQuery.data, "learning.dataset-quality-health.v2"), qualityHealthQuery.isError || qualityHealthQuery.isRefetchError),
  ].every(Boolean);
  const backtestJob = asRecord(backtestJobQuery.data);
  const backtestStatus = pickString(backtestJob, ["status"], backtestMutation.isPending ? "queued" : "");
  const backtestReport = pickRecord(backtestJob, ["result"]);
  const backtestMetrics = pickRecord(backtestReport, ["metrics"]);
  const learningBundle = pickRecord(backtestReport, ["learning_bundle"]);
  const learningBlockers = pickArray(learningBundle, ["blockers"]);

  return (
    <section className="dashboard models-dashboard">
      {!embedded ? <div className="dashboard-header">
        <div>
          <div className="eyebrow">模型学习</div>
          <h1>模型能力观察</h1>
          <p>只观察模型、样本准备度、审计轨迹和准入队列集中展示；这些模型不会直接接管交易或治理。</p>
        </div>
        <div className="header-status">
          <StatusPill status={modelFactsKnown ? (advisoryOnly ? "只建议/只观察" : "治理候选") : "模型事实待接入"} tone={modelFactsKnown ? (advisoryOnly ? "warn" : "ok") : "warn"} />
          <StatusPill status={`准入 ${translateDisplayValue(gateDecision)}`} tone={factBoundTone(readinessFact, statusTone(gateDecision), readinessRequestFailed)} />
          <StatusPill status={hasError ? "模型接口异常" : modelFactsKnown ? "模型链路在线" : isRefreshing ? "部分数据更新中" : "部分数据待确认"} tone={hasError ? "bad" : modelFactsKnown ? "ok" : "warn"} />
        </div>
      </div> : null}

      <div className="dashboard-grid">
        <MetricCard title="历史回测" className="wide-panel historical-backtest-panel">
          <div className="historical-backtest-layout">
            <form
              className="historical-backtest-form"
              onSubmit={(event) => {
                event.preventDefault();
                if (!backtestMutation.isPending && !["queued", "pending", "running"].includes(backtestStatus)) {
                  backtestMutation.mutate();
                }
              }}
            >
              <label>
                开始时间
                <input type="datetime-local" value={backtestForm.start} onChange={(event) => setBacktestForm((current) => ({ ...current, start: event.target.value }))} />
              </label>
              <label>
                结束时间
                <input type="datetime-local" value={backtestForm.end} onChange={(event) => setBacktestForm((current) => ({ ...current, end: event.target.value }))} />
              </label>
              <label>
                最多K线
                <input type="number" min={2} max={20000} value={backtestForm.maxBars} onChange={(event) => setBacktestForm((current) => ({ ...current, maxBars: Math.max(2, Math.min(20000, Number(event.target.value) || 5000)) }))} />
              </label>
              <div className="historical-backtest-action">
                <span>黄金 · 5分钟 · 单任务串行</span>
                <button className="primary-button" type="submit" disabled={backtestMutation.isPending || ["queued", "pending", "running"].includes(backtestStatus)}>
                  {["queued", "pending", "running"].includes(backtestStatus) ? "回测运行中" : "开始历史回测"}
                </button>
              </div>
            </form>

            <div className="historical-backtest-result" aria-live="polite">
              <div className="learning-section-head">
                <h3>运行结果</h3>
                <StatusPill status={backtestStatus ? translateDisplayValue(backtestStatus) : "尚未运行"} tone={backtestStatus ? statusTone(backtestStatus) : "mute"} />
              </div>
              <div className="model-mini-grid historical-backtest-metrics">
                <ModelMiniMetric label="进度" value={`${formatDecimal(pickNumber(backtestJob, ["progress_pct"], 0), 0)}%`} detail={pickString(backtestJob, ["current_step"], "")} tone={backtestStatus === "done" ? "ok" : "mute"} />
                <ModelMiniMetric label="K线" value={formatDecimal(pickNumber(backtestMetrics, ["bar_count"], 0), 0)} detail="闭合K线" />
                <ModelMiniMetric label="独立交易" value={formatDecimal(pickNumber(backtestMetrics, ["independent_trade_count"], 0), 0)} detail={`多 ${formatDecimal(pickNumber(backtestMetrics, ["long_trade_count"], 0), 0)} · 空 ${formatDecimal(pickNumber(backtestMetrics, ["short_trade_count"], 0), 0)}`} />
                <ModelMiniMetric label="净盈亏" value={formatDecimal(pickNumber(backtestMetrics, ["net_pnl"], 0), 2)} detail={`成本 ${formatDecimal(pickNumber(backtestMetrics, ["total_cost"], 0), 2)}（点差 ${formatDecimal(pickNumber(backtestMetrics, ["spread_cost"], 0), 2)} · 滑点 ${formatDecimal(pickNumber(backtestMetrics, ["slippage_cost"], 0), 2)}）`} tone={numberTone(pickNumber(backtestMetrics, ["net_pnl"], 0))} />
                <ModelMiniMetric label="胜率" value={formatPct(pickNumber(backtestMetrics, ["win_rate"], 0))} detail={`最大回撤 ${formatDecimal(pickNumber(backtestMetrics, ["max_drawdown_pct"], 0), 2)}%`} />
                <ModelMiniMetric label="可训练样本" value={`${formatDecimal(pickNumber(learningBundle, ["open_sample_count"], 0), 0)} / ${formatDecimal(pickNumber(learningBundle, ["factor_sample_count"], 0), 0)}`} detail={`开仓 / 因子 · 候选 ${formatDecimal(pickNumber(learningBundle, ["candidate_open_sample_count"], 0), 0)} · 排除 ${formatDecimal(pickNumber(learningBundle, ["excluded_trade_count"], 0), 0)}`} tone={pickBoolean(learningBundle, ["trainable"], false) ? "ok" : "warn"} />
              </div>
              {learningBlockers.length ? (
                <p className="historical-backtest-reason">
                  未进入训练：{learningBlockers.map(backtestReasonLabel).join("；")}
                </p>
              ) : backtestStatus === "done" ? (
                <p className="historical-backtest-ready">样本已隔离保存，可用于开仓质量和因子治理模型训练；晋级仍只认真实样本。</p>
              ) : null}
              {backtestMutation.isError || backtestJobQuery.isError ? <p className="historical-backtest-error">回测任务提交或读取失败，请查看后端任务错误。</p> : null}
            </div>
          </div>
        </MetricCard>

        <MetricCard title="模型能力与参与状态" className="wide-panel model-capability-panel">
          <div className="model-participation-summary">
            <div>
              <span>已登记模型</span>
              <strong>{modelRows.length}</strong>
            </div>
            <div>
              <span>当前影响交易</span>
              <strong>{participatingModelCount}</strong>
            </div>
            <div>
              <span>存在观察输出</span>
              <strong>{observedModelCount}</strong>
            </div>
            <p>
              {modelInfluenceEnabled
                ? "模型影响功能已启用；是否真正生效仍以每个模型的授权和准入结果为准。"
                : "模型影响功能当前未启用；模型可以训练和生成观察结果，但不会改变开仓、持仓或风控。"}
            </p>
          </div>
          <div className="table-wrap">
            <table className="mobile-card-table model-capability-table">
              <thead>
                <tr>
                  <th>模型 / 负责什么</th>
                  <th>当前是否参与</th>
                  <th>当前能力边界</th>
                  <th>为什么没有生效</th>
                  <th>最近输出</th>
                </tr>
              </thead>
              <tbody>
                {modelRows.map((model) => (
                  <tr key={model.type}>
                    <td className="model-capability-name" data-label="模型 / 负责什么">
                      <strong>{model.name}</strong>
                      <span>{model.purpose}</span>
                      {model.sourceSummary ? <small>{model.sourceSummary}</small> : null}
                    </td>
                    <td data-label="当前是否参与">
                      <StatusPill status={model.participation} tone={model.participationTone} />
                      <small>已应用 {formatDecimal(model.applied, 0)} 次</small>
                    </td>
                    <td data-label="当前能力边界">
                      <b>{model.permission}</b>
                    </td>
                    <td data-label="为什么没有生效">
                      <span>{model.affectsTrading ? "已通过当前授权边界" : model.reason}</span>
                    </td>
                    <td data-label="最近输出">
                      <b>{model.latestOutputAt > 0 ? formatTime(model.latestOutputAt) : "暂无输出"}</b>
                      <small>{formatDecimal(model.auditCount, 0)} 条已加载审计</small>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </MetricCard>

        <MetricCard title="模型验证与数据" className="wide-panel model-control-panel">
          <div className="model-mini-grid">
            <ModelMiniMetric label="治理模式" value={advisoryOnly ? "只建议/只观察" : "治理候选"} detail={translateDisplayValue(gateDecision)} tone={advisoryOnly ? "warn" : "ok"} />
            <ModelMiniMetric label="综合准确率" value={formatPct(accuracy)} detail={`${formatDecimal(evaluatedCount, 0)} 样本 · ${formatDecimal(auditCount, 0)} 审计`} tone={numberTone(accuracy - 0.5)} />
            <ModelMiniMetric label="独立样本验证" value={holdoutAccuracy > 0 ? formatPct(holdoutAccuracy) : ""} detail={`规则 ${formatPct(holdoutRuleAccuracy)} · 简单基线 ${formatPct(holdoutMajorityAccuracy)}`} tone={holdoutAccuracy >= 0.55 ? "ok" : "mute"} />
            <ModelMiniMetric label="样本准备" value={`${formatDecimal(modelReady, 0)}/${formatDecimal(sampleTotal || modelReady + needsAttention, 0)}`} detail={`${formatDecimal(needsAttention, 0)} 需处理`} tone={needsAttention > 0 ? "warn" : modelReady > 0 ? "ok" : "mute"} />
            <ModelMiniMetric label="开仓上下文" value={formatPct(Math.min(entryBarCoverage, entryExecutionCoverage, entryMicroCoverage))} detail={`${formatDecimal(entryOpenDecisions, 0)} 决策`} tone={entryContextStatus === "ok" ? "ok" : entryContextStatus === "warming" ? "mute" : "warn"} />
            <ModelMiniMetric label="数据检查异常" value={formatDecimal(evidenceBadTotal, 0)} detail={`${formatDecimal(pickNumber(evidenceCounts, ["checked"], 0), 0)} 已检查`} tone={evidenceBadTotal > 0 ? "bad" : "ok"} />
          </div>

          <div className="model-control-grid">
            <section className="model-control-section">
              <div className="learning-section-head">
                <h3>准入与权限</h3>
                <StatusPill status={translateDisplayValue(governanceReadinessStatus || gateDecision)} tone={statusTone(governanceReadinessStatus || gateDecision)} />
              </div>
              <div className="field-list model-compact-fields">
                <Field label="最低标准" value={translateDisplayValue(governanceReadinessStatus)} tone={statusTone(governanceReadinessStatus)} />
                <Field label="推荐来源" value={translateDisplayValue(recommendedSource)} />
                <Field label="实时权限" value={pickBoolean(capabilities, ["can_place_orders"], false) ? "允许下单" : "禁止下单"} tone={pickBoolean(capabilities, ["can_place_orders"], false) ? "bad" : "ok"} />
                <Field label="风险权限" value={pickBoolean(capabilities, ["can_change_risk_limits"], false) ? "允许改风控" : "禁止改风控"} tone={pickBoolean(capabilities, ["can_change_risk_limits"], false) ? "bad" : "ok"} />
                <Field label="未通过原因" value={degradationReason ? translateDisplayValue(degradationReason) : ""} />
              </div>
            </section>

            <section className="model-control-section">
              <div className="learning-section-head">
                <h3>只观察模型报告</h3>
                <StatusPill status={accuracy >= 0.55 ? "可观察" : "继续积累"} tone={accuracy >= 0.55 ? "ok" : "warn"} />
              </div>
              <div className="field-list model-compact-fields">
                <Field label="规则提升" value={formatPct(ruleLiftVsMajority)} tone={numberTone(ruleLiftVsMajority)} />
                <Field label="规则一致率" value={ruleAgreement > 0 ? formatPct(ruleAgreement) : ""} tone={ruleAgreement >= 0.55 ? "ok" : "mute"} />
                <Field label="预测分布" value={distributionSummary(pick(metaReport, ["posture_distribution"]))} />
                <Field label="标签分布" value={distributionSummary(pick(metaReport, ["label_distribution"]))} />
                <Field label="模型文件" value={shortText(pickString(artifactSummary, ["artifact_path"], ""), "", 58)} />
              </div>
            </section>

            <section className="model-control-section">
              <div className="learning-section-head">
                <h3>数据质量</h3>
                <StatusPill status={translateDisplayValue(entryContextStatus)} tone={statusTone(entryContextStatus)} />
              </div>
              <div className="field-list model-compact-fields">
                <Field label="同向簇" value={formatPct(pickNumber(entryCoverageRatio, ["entry_cluster"], 0))} />
                <Field label="K线 / 执行" value={`${formatPct(entryBarCoverage)} / ${formatPct(entryExecutionCoverage)}`} tone={entryBarCoverage >= 0.95 && entryExecutionCoverage >= 0.95 ? "ok" : "warn"} />
                <Field label="微观行情" value={formatPct(entryMicroCoverage)} tone={entryMicroCoverage >= 0.95 ? "ok" : "warn"} />
                <Field label="决策质量" value={formatPct(pickNumber(entryCoverageRatio, ["decision_quality_context"], 0))} />
                <Field label="盘外训练" value={translateDisplayValue(highLoadProfile)} tone={statusTone(highLoadProfile)} />
              </div>
            </section>
          </div>
        </MetricCard>

        <MetricCard title="模型工作台" className="wide-panel model-workbench-panel">
          <div className="model-workbench-grid">
            <div className="model-signal-zone">
              <section className="model-workbench-section">
                <div className="mini-section-title">综合模型重要特征</div>
                <div className="model-feature-list">
                  {!topFeatures.length ? <div className="empty-state-small">暂无特征解释</div> : null}
                  {topFeatures.slice(0, 10).map((raw, index) => {
                    const item = asRecord(raw);
                    const value = pickNumber(item, ["importance", "gain", "value", "weight"], 0);
                    const width = `${Math.max(8, Math.min(100, Math.abs(value) * 100))}%`;
                    return (
                      <div className="model-feature-row" key={keyFor(item, index, ["feature", "name"])}>
                        <div>
                          <strong>{pickString(item, ["feature", "name"], "")}</strong>
                          <span>{value > 0 ? "增强" : value < 0 ? "抑制" : "中性"}</span>
                        </div>
                        <b>{formatDecimal(value, 4)}</b>
                        <i style={{ width }} />
                      </div>
                    );
                  })}
                </div>
              </section>

              <div className="model-signal-side">
                <section className="model-workbench-section">
                  <div className="mini-section-title">综合模型建议记录</div>
                  <div className="model-event-list">
                    {!metaAdvisories.length ? <div className="empty-state-small">暂无顾问记录</div> : null}
                    {metaAdvisories.slice(0, 6).map((raw, index) => {
                      const item = asRecord(raw);
                      const status = pickString(item, ["status", "decision", "posture"], "");
                      return (
                        <ModelEventItem
                          key={keyFor(item, index, ["advisory_id", "decision_id", "id"])}
                          title={translateDisplayValue(pickString(item, ["posture", "target_posture"], ""))}
                          meta={formatTime(pick(item, ["created_at", "ts"]))}
                          status={status}
                          detail={shortText(pickString(item, ["summary", "reason", "rationale"], ""), "", 140)}
                        />
                      );
                    })}
                  </div>
                </section>

                <section className="model-workbench-section">
                  <div className="mini-section-title">观察候选与小范围验证</div>
                  <div className="model-event-list">
                    {!shadowQueue.length && !canaryReviews.length ? <div className="empty-state-small">暂无观察候选 · 暂无小范围验证审查</div> : null}
                    {shadowQueue.slice(0, 8).map((raw, index) => {
                      const item = asRecord(raw);
                      const status = pickString(item, ["status"], "");
                      const gate = pickString(item, ["gate_decision", "gate.decision"], "");
                      return (
                        <ModelEventItem
                          key={keyFor(item, index, ["candidate_id"])}
                          title={pickString(item, ["model_type"], "")}
                          meta={`${formatTime(pick(item, ["updated_at", "created_at"]))} · 准入 ${translateDisplayValue(gate)}`}
                          status={status}
                          detail={shortText(pickString(item, ["note", "candidate_id"], ""), "", 140)}
                        />
                      );
                    })}
                    {canaryReviews.slice(0, 8).map((raw, index) => {
                      const item = asRecord(raw);
                      const decision = pickString(item, ["decision", "status"], "");
                      return (
                        <ModelEventItem
                          key={keyFor(item, index, ["review_id", "candidate_id"])}
                          title={shortText(pickString(item, ["candidate_id"], ""), "", 44)}
                          meta={formatTime(pick(item, ["created_at", "updated_at"]))}
                          status={decision}
                          score={formatPct(pickNumber(item, ["accuracy", "metrics.accuracy"], 0))}
                          detail={shortText(pickString(item, ["reason", "note", "summary"], ""), "", 140)}
                        />
                      );
                    })}
                  </div>
                </section>

                <section className="model-workbench-section">
                  <div className="mini-section-title">权限与推理审计</div>
                  <div className="model-event-list model-audit-list">
                    {[...permissionAudits.slice(0, 5), ...inferenceAudits.slice(0, 5)].length ? null : (
                      <div className="empty-state-small">暂无权限或推理审计</div>
                    )}
                    {[...permissionAudits.slice(0, 5), ...inferenceAudits.slice(0, 5)].map((raw, index) => {
                      const item = asRecord(raw);
                      const status = pickString(item, ["status", "decision", "allowed"], "");
                      const source = index < Math.min(permissionAudits.length, 5) ? "权限" : "推理";
                      return (
                        <ModelEventItem
                          key={keyFor(item, index, ["audit_id", "inference_id", "candidate_id", "id"])}
                          title={`${source} · ${shortText(pickString(item, ["model_type", "candidate_id"], ""), "", 48)}`}
                          meta={formatTime(pick(item, ["created_at", "event_ts", "updated_at"]))}
                          status={status}
                          detail={shortText(pickString(item, ["reason", "error", "note"], objectSummary(pickRecord(item, ["result", "verdict"]))), "", 150)}
                        />
                      );
                    })}
                  </div>
                </section>
              </div>
            </div>

            <section className="model-workbench-section model-audit-zone">
              <div className="mini-section-title">综合 / 仓位 / 因子只观察审计</div>
              <div className="model-event-list model-audit-list">
                {[...metaAudits.slice(0, 3), ...openAudits.slice(0, 3), ...positionAudits.slice(0, 3), ...factorAudits.slice(0, 3)].length ? null : (
                  <div className="empty-state-small">暂无模型审计</div>
                )}
                {[...metaAudits.slice(0, 3), ...openAudits.slice(0, 3), ...positionAudits.slice(0, 3), ...factorAudits.slice(0, 3)].map((raw, index) => {
                  const item = asRecord(raw);
                  const modelType = pickString(item, ["model_type", "type"], "shadow_model");
                  const score = pickNumber(item, ["posture_score", "score", "quality_score", "weakness_score", "confidence"], 0);
                  const output = translateDisplayValue(pickString(item, ["posture", "label", "decision", "status"], ""));
                  return (
                    <ModelEventItem
                      key={keyFor(item, index, ["inference_id", "audit_id", "id", "position_id", "factor"])}
                      title={translateDisplayValue(modelType)}
                      meta={`${formatTime(pick(item, ["created_at", "event_ts", "updated_at"]))} · ${shortText(pickString(item, ["position_id", "factor", "sample_id", "target_position_id"], ""), "", 52)}`}
                      status={output}
                      tone={statusTone(output)}
                      score={formatDecimal(score, 4)}
                    />
                  );
                })}
              </div>
            </section>
          </div>
        </MetricCard>

        {hasError ? (
          <MetricCard title="模型接口异常" className="wide-panel">
            <QueryErrorList queries={[
              { label: "backend-readiness", query: readinessQuery },
              { label: "dataset/readiness", query: datasetQuery },
              { label: "meta-lightgbm/report", query: metaReportQuery },
              { label: "meta-lightgbm/audits", query: metaAuditsQuery },
              { label: "position-quality/audits", query: positionAuditsQuery },
              { label: "open-quality/audits", query: openAuditsQuery },
              { label: "factor-governance/audits", query: factorAuditsQuery },
              { label: "factor-governance/advisories", query: factorAdvisoriesQuery },
              { label: "shadow-queue", query: shadowQueueQuery },
              { label: "canary-reviews", query: canaryQuery },
              { label: "inference-audits", query: inferenceQuery },
              { label: "permissions", query: permissionsQuery },
              { label: "meta-advisories", query: metaAdvisoriesQuery },
              { label: "offmarket-high-load", query: highLoadAuditsQuery },
              { label: "dataset-quality", query: qualityHealthQuery },
            ]} />
          </MetricCard>
        ) : null}
      </div>
    </section>
  );
}
