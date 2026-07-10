import { useQuery } from "@tanstack/react-query";
import {
  BrainCircuit,
  CircleGauge,
  GitBranch,
  ShieldCheck,
} from "lucide-react";
import { MetricCard } from "@/components/Card";
import { CompactMetric as ModelMiniMetric, Field, StatTile, numberTone, toneFromStatus, type Tone } from "@/components/DashboardBits";
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
} from "@/api/client";
import { asRecord, pick, pickArray, pickBoolean, pickNumber, pickRecord, pickString } from "@/lib/compat";
import { translateDisplayValue } from "@/lib/display";
import { formatDecimal, formatTime } from "@/lib/format";
import { useBackendReadinessQuery } from "@/hooks/useCoreQueries";

function formatPct(value: number): string {
  return `${formatDecimal(value * 100, 1)}%`;
}

function shortText(value: unknown, fallback = "--", maxLength = 96): string {
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
  if (!value) return "--";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return shortText(JSON.stringify(value), "--", 140);
  } catch {
    return "--";
  }
}

function distributionSummary(record: unknown): string {
  const dist = asRecord(record);
  const entries = Object.entries(dist)
    .filter(([, value]) => Number(value) > 0)
    .map(([key, value]) => `${translateDisplayValue(key)} ${formatDecimal(Number(value), 0)}`);
  return entries.length ? entries.join(" · ") : "--";
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

export function ModelsPage() {
  const readinessQuery = useBackendReadinessQuery(60_000);
  const datasetQuery = useQuery({
    queryKey: ["learning-dataset-readiness"],
    queryFn: getLearningDatasetReadiness,
    refetchInterval: 60_000,
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
  const models = asRecord(pick(readiness, ["models"]));
  const metaLightgbm = asRecord(pick(models, ["meta_lightgbm"]));
  const promotionGate = asRecord(pick(metaLightgbm, ["promotion_gate"]));
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
  const factorAdvisories = pickArray(factorAdvisoriesQuery.data, ["items", "advisories"]);
  const shadowQueue = pickArray(shadowQueueQuery.data, ["items"]);
  const canaryReviews = pickArray(canaryQuery.data, ["items"]);
  const inferenceAudits = pickArray(inferenceQuery.data, ["items"]);
  const permissionAudits = pickArray(permissionsQuery.data, ["items"]);
  const metaAdvisories = pickArray(metaAdvisoriesQuery.data, ["items"]);
  const highLoadAudits = pickArray(highLoadAuditsQuery.data, ["items"]);
  const topFeatures = pickArray(artifactSummary, ["top_features"]);
  const qualityHealth = asRecord(qualityHealthQuery.data);
  const entryContext = asRecord(pick(qualityHealth, ["entry_context"]));
  const entryCoverageRatio = asRecord(pick(entryContext, ["coverage_ratio"]));
  const entrySamples = asRecord(pick(entryContext, ["samples"]));
  const evidenceHealth = asRecord(pick(qualityHealth, ["evidence_contract"]));
  const evidenceCounts = asRecord(pick(evidenceHealth, ["counts"]));

  const evaluatedCount = pickNumber(metaReport, ["evaluated_count"], pickNumber(metaLightgbm, ["report.evaluated_count"], 0));
  const auditCount = pickNumber(metaReport, ["audit_count"], countItems(metaAuditsQuery.data));
  const accuracy = pickNumber(metaReport, ["accuracy"], pickNumber(metaLightgbm, ["report.accuracy"], 0));
  const holdoutAccuracy = pickNumber(holdoutMetrics, ["accuracy"], 0);
  const holdoutRuleAccuracy = pickNumber(holdoutMetrics, ["rule_accuracy"], 0);
  const holdoutMajorityAccuracy = pickNumber(holdoutMetrics, ["majority_baseline_accuracy"], 0);
  const ruleLiftVsMajority = pickNumber(holdoutMetrics, ["rule_lift_vs_majority"], 0);
  const governanceReadinessStatus = pickString(governanceReadiness, ["status"], "unknown");
  const recommendedSource = pickString(governanceReadiness, ["recommended_source"], "--");
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
  const entryModelReady = pickNumber(entrySamples, ["model_ready_open_outcome"], 0);
  const evidenceBadTotal = pickNumber(evidenceCounts, ["bad_total"], 0);
  const gateEligible = pickBoolean(promotionGate, ["eligible_for_live", "eligible_for_governor_review", "ok"], false);
  const gateDecision = pickString(promotionGate, ["decision", "status"], gateEligible ? "eligible" : "shadow_only");
  const highLoadProfile = pickString(highLoad, ["profile", "status"], "--");
  const advisoryOnly = pickBoolean(capabilities, ["advisory_only"], true);

  const modelCards = [
    {
      name: "Meta LightGBM",
      role: "全局姿态影子模型",
      status: evaluatedCount > 0 ? "shadow" : "warming",
      metric: formatPct(accuracy),
      detail: `${formatDecimal(evaluatedCount, 0)} 条评估 · ${formatDecimal(auditCount, 0)} 条审计`,
    },
    {
      name: "Open Quality LightGBM",
      role: "开仓时机影子评分",
      status: openAudits.length > 0 ? "shadow" : "warming",
      metric: formatDecimal(countItems(openAuditsQuery.data), 0),
      detail: `${formatDecimal(entryModelReady, 0)} 条 open outcome 可训练`,
    },
    {
      name: "Position Quality LightGBM",
      role: "仓位质量评分",
      status: positionAudits.length > 0 ? "shadow" : "warming",
      metric: formatDecimal(countItems(positionAuditsQuery.data), 0),
      detail: "最近仓位质量影子审计",
    },
    {
      name: "Factor Governance LightGBM",
      role: "因子弱化与建议",
      status: factorAudits.length > 0 || factorAdvisories.length > 0 ? "advisory" : "warming",
      metric: formatDecimal(countItems(factorAuditsQuery.data), 0),
      detail: `${formatDecimal(factorAdvisories.length, 0)} 条因子建议`,
    },
    {
      name: "Off-market High-load",
      role: "盘外高负载学习",
      status: highLoadProfile,
      metric: formatDecimal(countItems(highLoadAuditsQuery.data), 0),
      detail: "训练/批处理审计",
    },
  ];

  const hasError = [
    readinessQuery,
    datasetQuery,
    metaReportQuery,
    metaAuditsQuery,
    positionAuditsQuery,
    openAuditsQuery,
    factorAuditsQuery,
    shadowQueueQuery,
    canaryQuery,
    inferenceQuery,
    permissionsQuery,
    qualityHealthQuery,
  ].some((query) => query.isError);

  return (
    <section className="dashboard models-dashboard">
      <div className="dashboard-header">
        <div>
          <div className="eyebrow">模型学习</div>
          <h1>模型能力观察</h1>
          <p>影子模型、样本准备度、审计轨迹和准入队列集中展示；当前只观察，不直接接管治理。</p>
        </div>
        <div className="header-status">
          <StatusPill status={advisoryOnly ? "顾问/影子模式" : "治理候选"} tone={advisoryOnly ? "warn" : "ok"} />
          <StatusPill status={`门控 ${translateDisplayValue(gateDecision)}`} tone={statusTone(gateDecision)} />
          <StatusPill status={hasError ? "模型接口异常" : "模型链路在线"} tone={hasError ? "bad" : "ok"} />
        </div>
      </div>

      <div className="stat-grid">
        <StatTile
          icon={BrainCircuit}
          label="Meta 准确率"
          value={formatPct(accuracy)}
          detail={`${formatDecimal(evaluatedCount, 0)} 条可评估样本`}
          tone={numberTone(accuracy - 0.5)}
        />
        <StatTile
          icon={CircleGauge}
          label="开仓上下文"
          value={formatPct(Math.min(entryBarCoverage, entryExecutionCoverage, entryMicroCoverage))}
          detail={`${formatDecimal(entryOpenDecisions, 0)} 条开仓 · ${translateDisplayValue(entryContextStatus)}`}
          tone={entryContextStatus === "ok" ? "ok" : entryContextStatus === "warming" ? "mute" : "warn"}
        />
        <StatTile
          icon={GitBranch}
          label="影子队列"
          value={formatDecimal(countItems(shadowQueueQuery.data), 0)}
          detail={`Canary ${formatDecimal(countItems(canaryQuery.data), 0)} · 推理 ${formatDecimal(countItems(inferenceQuery.data), 0)}`}
          tone={shadowQueue.length > 0 || canaryReviews.length > 0 ? "warn" : "mute"}
        />
        <StatTile
          icon={ShieldCheck}
          label="权限审计"
          value={formatDecimal(countItems(permissionsQuery.data), 0)}
          detail={translateDisplayValue(governanceReadinessStatus || (gateEligible ? "eligible" : "shadow_only"))}
          tone={statusTone(governanceReadinessStatus || (gateEligible ? "eligible" : "shadow_only"))}
        />
      </div>

      <div className="dashboard-grid">
        <MetricCard title="模型运行控制台" className="wide-panel model-control-panel">
          <div className="model-mini-grid">
            <ModelMiniMetric label="治理模式" value={advisoryOnly ? "影子顾问" : "治理候选"} detail={translateDisplayValue(gateDecision)} tone={advisoryOnly ? "warn" : "ok"} />
            <ModelMiniMetric label="Meta 准确率" value={formatPct(accuracy)} detail={`${formatDecimal(evaluatedCount, 0)} 样本 · ${formatDecimal(auditCount, 0)} 审计`} tone={numberTone(accuracy - 0.5)} />
            <ModelMiniMetric label="Holdout" value={holdoutAccuracy > 0 ? formatPct(holdoutAccuracy) : "--"} detail={`规则 ${formatPct(holdoutRuleAccuracy)} · 基线 ${formatPct(holdoutMajorityAccuracy)}`} tone={holdoutAccuracy >= 0.55 ? "ok" : "mute"} />
            <ModelMiniMetric label="样本准备" value={`${formatDecimal(modelReady, 0)}/${formatDecimal(sampleTotal || modelReady + needsAttention, 0)}`} detail={`${formatDecimal(needsAttention, 0)} 需处理`} tone={needsAttention > 0 ? "warn" : modelReady > 0 ? "ok" : "mute"} />
            <ModelMiniMetric label="开仓上下文" value={formatPct(Math.min(entryBarCoverage, entryExecutionCoverage, entryMicroCoverage))} detail={`${formatDecimal(entryOpenDecisions, 0)} 决策`} tone={entryContextStatus === "ok" ? "ok" : entryContextStatus === "warming" ? "mute" : "warn"} />
            <ModelMiniMetric label="契约异常" value={formatDecimal(evidenceBadTotal, 0)} detail={`${formatDecimal(pickNumber(evidenceCounts, ["checked"], 0), 0)} 已检查`} tone={evidenceBadTotal > 0 ? "bad" : "ok"} />
          </div>

          <div className="model-control-grid">
            <section className="model-control-section">
              <div className="learning-section-head">
                <h3>门控与权限</h3>
                <StatusPill status={translateDisplayValue(governanceReadinessStatus || gateDecision)} tone={statusTone(governanceReadinessStatus || gateDecision)} />
              </div>
              <div className="field-list model-compact-fields">
                <Field label="基线保护" value={translateDisplayValue(governanceReadinessStatus)} tone={statusTone(governanceReadinessStatus)} />
                <Field label="推荐来源" value={translateDisplayValue(recommendedSource)} />
                <Field label="实时权限" value={pickBoolean(capabilities, ["can_place_orders"], false) ? "允许下单" : "禁止下单"} tone={pickBoolean(capabilities, ["can_place_orders"], false) ? "bad" : "ok"} />
                <Field label="风险权限" value={pickBoolean(capabilities, ["can_change_risk_limits"], false) ? "允许改风控" : "禁止改风控"} tone={pickBoolean(capabilities, ["can_change_risk_limits"], false) ? "bad" : "ok"} />
                <Field label="退化原因" value={degradationReason ? translateDisplayValue(degradationReason) : "--"} />
              </div>
            </section>

            <section className="model-control-section">
              <div className="learning-section-head">
                <h3>影子报告</h3>
                <StatusPill status={accuracy >= 0.55 ? "可观察" : "继续积累"} tone={accuracy >= 0.55 ? "ok" : "warn"} />
              </div>
              <div className="field-list model-compact-fields">
                <Field label="规则提升" value={formatPct(ruleLiftVsMajority)} tone={numberTone(ruleLiftVsMajority)} />
                <Field label="规则一致率" value={ruleAgreement > 0 ? formatPct(ruleAgreement) : "--"} tone={ruleAgreement >= 0.55 ? "ok" : "mute"} />
                <Field label="预测分布" value={distributionSummary(pick(metaReport, ["posture_distribution"]))} />
                <Field label="标签分布" value={distributionSummary(pick(metaReport, ["label_distribution"]))} />
                <Field label="模型文件" value={shortText(pickString(artifactSummary, ["artifact_path"], "--"), "--", 58)} />
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
            <div className="model-workbench-column model-family-column">
              <section className="model-workbench-section">
                <div className="mini-section-title">模型族能力</div>
                <div className="model-card-grid model-card-grid-compact">
                  {modelCards.map((model) => (
                    <div className="model-family-card" key={model.name}>
                      <div className="model-family-head">
                        <div>
                          <strong>{model.name}</strong>
                          <span>{model.role}</span>
                        </div>
                        <StatusPill status={model.status} tone={statusTone(model.status)} />
                      </div>
                      <div className="model-family-metric">{model.metric}</div>
                      <div className="model-family-detail">{model.detail}</div>
                    </div>
                  ))}
                </div>
              </section>
            </div>

            <div className="model-signal-zone">
              <section className="model-workbench-section">
                <div className="mini-section-title">Meta 重要特征</div>
                <div className="model-feature-list">
                  {!topFeatures.length ? <div className="empty-state-small">暂无特征解释</div> : null}
                  {topFeatures.slice(0, 10).map((raw, index) => {
                    const item = asRecord(raw);
                    const value = pickNumber(item, ["importance", "gain", "value", "weight"], 0);
                    const width = `${Math.max(8, Math.min(100, Math.abs(value) * 100))}%`;
                    return (
                      <div className="model-feature-row" key={keyFor(item, index, ["feature", "name"])}>
                        <div>
                          <strong>{pickString(item, ["feature", "name"], "--")}</strong>
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
                  <div className="mini-section-title">Meta 顾问记录</div>
                  <div className="model-event-list">
                    {!metaAdvisories.length ? <div className="empty-state-small">暂无顾问记录</div> : null}
                    {metaAdvisories.slice(0, 6).map((raw, index) => {
                      const item = asRecord(raw);
                      const status = pickString(item, ["status", "decision", "posture"], "--");
                      return (
                        <ModelEventItem
                          key={keyFor(item, index, ["advisory_id", "decision_id", "id"])}
                          title={translateDisplayValue(pickString(item, ["posture", "target_posture"], "--"))}
                          meta={formatTime(pick(item, ["created_at", "ts"]))}
                          status={status}
                          detail={shortText(pickString(item, ["summary", "reason", "rationale"], "--"), "--", 140)}
                        />
                      );
                    })}
                  </div>
                </section>

                <section className="model-workbench-section">
                  <div className="mini-section-title">影子队列与 Canary</div>
                  <div className="model-event-list">
                    {!shadowQueue.length && !canaryReviews.length ? <div className="empty-state-small">暂无影子候选 · 暂无 Canary 审查</div> : null}
                    {shadowQueue.slice(0, 8).map((raw, index) => {
                      const item = asRecord(raw);
                      const status = pickString(item, ["status"], "--");
                      const gate = pickString(item, ["gate_decision", "gate.decision"], "--");
                      return (
                        <ModelEventItem
                          key={keyFor(item, index, ["candidate_id"])}
                          title={pickString(item, ["model_type"], "--")}
                          meta={`${formatTime(pick(item, ["updated_at", "created_at"]))} · 门控 ${translateDisplayValue(gate)}`}
                          status={status}
                          detail={shortText(pickString(item, ["note", "candidate_id"], "--"), "--", 140)}
                        />
                      );
                    })}
                    {canaryReviews.slice(0, 8).map((raw, index) => {
                      const item = asRecord(raw);
                      const decision = pickString(item, ["decision", "status"], "--");
                      return (
                        <ModelEventItem
                          key={keyFor(item, index, ["review_id", "candidate_id"])}
                          title={shortText(pickString(item, ["candidate_id"], "--"), "--", 44)}
                          meta={formatTime(pick(item, ["created_at", "updated_at"]))}
                          status={decision}
                          score={formatPct(pickNumber(item, ["accuracy", "metrics.accuracy"], 0))}
                          detail={shortText(pickString(item, ["reason", "note", "summary"], "--"), "--", 140)}
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
                      const status = pickString(item, ["status", "decision", "allowed"], "--");
                      const source = index < Math.min(permissionAudits.length, 5) ? "权限" : "推理";
                      return (
                        <ModelEventItem
                          key={keyFor(item, index, ["audit_id", "inference_id", "candidate_id", "id"])}
                          title={`${source} · ${shortText(pickString(item, ["model_type", "candidate_id"], "--"), "--", 48)}`}
                          meta={formatTime(pick(item, ["created_at", "event_ts", "updated_at"]))}
                          status={status}
                          detail={shortText(pickString(item, ["reason", "error", "note"], objectSummary(pickRecord(item, ["result", "verdict"]))), "--", 150)}
                        />
                      );
                    })}
                  </div>
                </section>
              </div>
            </div>

            <section className="model-workbench-section model-audit-zone">
              <div className="mini-section-title">Meta / 仓位 / 因子影子审计</div>
              <div className="model-event-list model-audit-list">
                {[...metaAudits.slice(0, 3), ...openAudits.slice(0, 3), ...positionAudits.slice(0, 3), ...factorAudits.slice(0, 3)].length ? null : (
                  <div className="empty-state-small">暂无模型审计</div>
                )}
                {[...metaAudits.slice(0, 3), ...openAudits.slice(0, 3), ...positionAudits.slice(0, 3), ...factorAudits.slice(0, 3)].map((raw, index) => {
                  const item = asRecord(raw);
                  const modelType = pickString(item, ["model_type", "type"], "shadow_model");
                  const score = pickNumber(item, ["posture_score", "score", "quality_score", "weakness_score", "confidence"], 0);
                  const output = translateDisplayValue(pickString(item, ["posture", "label", "decision", "status"], "--"));
                  return (
                    <ModelEventItem
                      key={keyFor(item, index, ["inference_id", "audit_id", "id", "position_id", "factor"])}
                      title={translateDisplayValue(modelType)}
                      meta={`${formatTime(pick(item, ["created_at", "event_ts", "updated_at"]))} · ${shortText(pickString(item, ["position_id", "factor", "sample_id", "target_position_id"], "--"), "--", 52)}`}
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
            <ul className="error-list">
              {readinessQuery.isError ? <li>readiness：{readinessQuery.error instanceof Error ? readinessQuery.error.message : "请求失败"}</li> : null}
              {datasetQuery.isError ? <li>dataset/readiness：{datasetQuery.error instanceof Error ? datasetQuery.error.message : "请求失败"}</li> : null}
              {metaReportQuery.isError ? <li>meta-lightgbm/report：{metaReportQuery.error instanceof Error ? metaReportQuery.error.message : "请求失败"}</li> : null}
              {shadowQueueQuery.isError ? <li>shadow-queue：{shadowQueueQuery.error instanceof Error ? shadowQueueQuery.error.message : "请求失败"}</li> : null}
              {permissionsQuery.isError ? <li>permissions：{permissionsQuery.error instanceof Error ? permissionsQuery.error.message : "请求失败"}</li> : null}
            </ul>
          </MetricCard>
        ) : null}
      </div>
    </section>
  );
}
