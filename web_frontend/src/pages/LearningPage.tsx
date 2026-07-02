import { useQuery } from "@tanstack/react-query";
import { BrainCircuit, FileCheck2, GitBranch, GraduationCap, Layers3, ShieldCheck } from "lucide-react";
import { MetricCard } from "@/components/Card";
import { Field, StatTile, numberTone, toneFromStatus } from "@/components/DashboardBits";
import { StatusPill } from "@/components/StatusPill";
import {
  getAutonomousLearningSamples,
  getBackendReadiness,
  getLearningApplications,
  getLearningLifecycle,
  getLearningReviews,
  getLearningSummary,
  getLearningSuggestions,
  getMetaLightgbmShadowReport,
  getModelPermissionAudits,
} from "@/api/client";
import { asRecord, pick, pickArray, pickBoolean, pickNumber, pickRecord, pickString } from "@/lib/compat";
import { translateDisplayValue, translateReasonText, translateScopeLabel } from "@/lib/display";
import { formatDecimal, formatTime } from "@/lib/format";

function countFrom(record: unknown, key: string): number {
  return pickNumber(record, [key], 0);
}

function formatPct(value: number): string {
  return `${formatDecimal(value * 100, 1)}%`;
}

function shortText(value: unknown, fallback = "--"): string {
  const text = String(value || "").trim();
  if (!text) return fallback;
  return text.length > 120 ? `${text.slice(0, 120)}...` : text;
}

export function LearningPage() {
  const summaryQuery = useQuery({
    queryKey: ["learning-summary"],
    queryFn: getLearningSummary,
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
  const suggestionsQuery = useQuery({
    queryKey: ["learning-suggestions"],
    queryFn: () => getLearningSuggestions(20),
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
  const applicationsQuery = useQuery({
    queryKey: ["learning-applications"],
    queryFn: () => getLearningApplications(20),
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
  const lifecycleQuery = useQuery({
    queryKey: ["learning-lifecycle"],
    queryFn: () => getLearningLifecycle(30),
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
  const reviewsQuery = useQuery({
    queryKey: ["learning-reviews"],
    queryFn: () => getLearningReviews(10),
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
  const samplesQuery = useQuery({
    queryKey: ["learning-autonomous-samples"],
    queryFn: () => getAutonomousLearningSamples(10),
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
  const metaReportQuery = useQuery({
    queryKey: ["learning-meta-lightgbm-report"],
    queryFn: () => getMetaLightgbmShadowReport(80),
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const permissionQuery = useQuery({
    queryKey: ["learning-model-permissions"],
    queryFn: () => getModelPermissionAudits(10),
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
  const readinessQuery = useQuery({
    queryKey: ["backend-readiness", "learning"],
    queryFn: getBackendReadiness,
    refetchInterval: 30_000,
    staleTime: 10_000,
  });

  const summary = asRecord(summaryQuery.data);
  const suggestionCounts = asRecord(pick(summary, ["suggestions"]));
  const reviewCounts = asRecord(pick(summary, ["reviews"]));
  const candidateCounts = asRecord(pick(summary, ["parameter_template_candidates"]));
  const recommendationCounts = asRecord(pick(summary, ["parameter_template_recommendations"]));
  const latestReview = asRecord(pick(summary, ["latest_review"]));
  const todo = asRecord(pick(summary, ["parameter_template_todo"]));
  const overview = asRecord(pick(summary, ["parameter_template_overview"]));
  const readiness = asRecord(readinessQuery.data);
  const governance = asRecord(pick(readiness, ["governance"]));
  const factorData = asRecord(pick(readiness, ["factor_data"]));
  const factorState = asRecord(pick(factorData, ["state"]));
  const factorHealth = asRecord(pick(factorState, ["factor_health_by_status"]));
  const models = asRecord(pick(readiness, ["models"]));
  const metaLightgbm = asRecord(pick(models, ["meta_lightgbm"]));
  const promotionGate = asRecord(pick(metaLightgbm, ["promotion_gate"]));
  const highLoad = asRecord(pick(readiness, ["high_load"]));

  const suggestions = pickArray(suggestionsQuery.data, ["items"]);
  const applications = pickArray(applicationsQuery.data, ["items"]);
  const lifecycle = pickArray(lifecycleQuery.data, ["items"]);
  const reviews = pickArray(reviewsQuery.data, ["items"]);
  const samples = pickArray(samplesQuery.data, ["items"]);
  const permissions = pickArray(permissionQuery.data, ["items"]);
  const metaReport = asRecord(metaReportQuery.data);

  const proposed = countFrom(suggestionCounts, "proposed");
  const approved = countFrom(suggestionCounts, "approved");
  const applied = countFrom(suggestionCounts, "applied");
  const rolledBack = countFrom(suggestionCounts, "rolled_back");
  const pendingCandidates = countFrom(candidateCounts, "pending_review");
  const applicationsCount = pickNumber(summary, ["applications"], applications.length);
  const sampleCount = pickNumber(samplesQuery.data, ["count"], samples.length);
  const totalFactors = pickNumber(factorState, ["factor_health_total"], 0);
  const healthyFactors = countFrom(factorHealth, "HEALTHY");
  const watchFactors = countFrom(factorHealth, "WATCH");
  const modelAccuracy = pickNumber(metaReport, ["accuracy"], pickNumber(metaLightgbm, ["report.accuracy"], 0));
  const evaluatedCount = pickNumber(metaReport, ["evaluated_count"], pickNumber(metaLightgbm, ["report.evaluated_count"], 0));
  const modelEligible = pickBoolean(promotionGate, ["eligible_for_live", "eligible_for_governor_review"], false);
  const automaticExecution = pickBoolean(governance, ["automatic_execution_enabled"], false);
  const autonomyMode = pickString(governance, ["autonomy_mode"], "unknown");
  const latestFactorUpdate = asRecord(pick(factorData, ["last_enrichment"]));
  const highLoadProfile = pickString(highLoad, ["profile"], "--");

  const hasError = [
    summaryQuery,
    suggestionsQuery,
    applicationsQuery,
    lifecycleQuery,
    reviewsQuery,
    samplesQuery,
    metaReportQuery,
    permissionQuery,
    readinessQuery,
  ].some((query) => query.isError);

  return (
    <section className="dashboard learning-dashboard">
      <div className="dashboard-header">
        <div>
          <div className="eyebrow">学习闭环</div>
          <h1>学习与治理</h1>
          <p>复盘、样本、策略建议、参数治理和影子模型统一在这里观察。</p>
        </div>
        <div className="header-status">
          <StatusPill status={automaticExecution ? "自动应用已开" : "人工审核"} tone={automaticExecution ? "warn" : "ok"} />
          <StatusPill status={`模式 ${translateDisplayValue(autonomyMode)}`} tone="mute" />
          <StatusPill status={hasError ? "接口异常" : "学习链路在线"} tone={hasError ? "bad" : "ok"} />
        </div>
      </div>

      <div className="stat-grid">
        <StatTile
          icon={BrainCircuit}
          label="待治理建议"
          value={formatDecimal(proposed + pendingCandidates, 0)}
          detail={`建议 ${formatDecimal(proposed, 0)} · 候选 ${formatDecimal(pendingCandidates, 0)}`}
          tone={proposed + pendingCandidates > 0 ? "warn" : "ok"}
        />
        <StatTile
          icon={GraduationCap}
          label="学习样本"
          value={formatDecimal(sampleCount, 0)}
          detail={`复盘 ${formatDecimal(reviews.length, 0)} · 应用 ${formatDecimal(applicationsCount, 0)}`}
          tone={sampleCount > 0 ? "ok" : "mute"}
        />
        <StatTile
          icon={Layers3}
          label="因子健康"
          value={formatDecimal(totalFactors, 0)}
          detail={`健康 ${formatDecimal(healthyFactors, 0)} · 观察 ${formatDecimal(watchFactors, 0)}`}
          tone={healthyFactors > 0 ? "ok" : "warn"}
        />
        <StatTile
          icon={ShieldCheck}
          label="影子模型"
          value={formatPct(modelAccuracy)}
          detail={`${formatDecimal(evaluatedCount, 0)} 条评估 · ${modelEligible ? "可审" : "仅观察"}`}
          tone={numberTone(modelAccuracy - 0.5)}
        />
      </div>

      <div className="dashboard-grid">
        <MetricCard title="学习闭环摘要">
          <div className="field-list">
            <Field label="治理摘要" value={shortText(pickString(summary, ["parameter_template_ops_summary"], "--"), "--")} />
            <Field label="概览" value={shortText(pickString(overview, ["headline"], "--"), "--")} />
            <Field label="当前任务" value={shortText(pickString(todo, ["title", "summary"], "--"), "--")} />
            <Field label="建议状态" value={`待审核 ${proposed} / 已批准 ${approved} / 已应用 ${applied} / 回滚 ${rolledBack}`} />
            <Field label="推荐" value={`总计 ${countFrom(recommendationCounts, "total")} · 在线 ${countFrom(recommendationCounts, "online_light")} · 离线 ${countFrom(recommendationCounts, "offline_deep")}`} />
            <Field label="最近复盘" value={latestReview.review_id ? `${translateDisplayValue(pickString(latestReview, ["outcome_label"], "--"))} · ${formatDecimal(pickNumber(latestReview, ["pnl"], 0), 2)}` : "--"} />
          </div>
        </MetricCard>

        <MetricCard title="模型与权限">
          <div className="field-list">
            <Field label="Meta LightGBM" value={`${formatPct(modelAccuracy)} · ${formatDecimal(evaluatedCount, 0)} 条评估`} tone={modelAccuracy >= 0.6 ? "ok" : "warn"} />
            <Field label="模型门控" value={modelEligible ? "可进入治理审查" : "影子/顾问模式"} tone={modelEligible ? "ok" : "mute"} />
            <Field label="高负载训练" value={translateDisplayValue(highLoadProfile)} tone={toneFromStatus(highLoadProfile)} />
            <Field label="权限审计" value={`${permissions.length} 条最近记录`} />
            <Field label="因子更新" value={formatTime(pick(latestFactorUpdate, ["updated_at", "ts"]))} tone={pickBoolean(latestFactorUpdate, ["ok"], true) ? "ok" : "bad"} />
          </div>
        </MetricCard>

        <MetricCard title="最近策略建议" className="wide-panel">
          <div className="table-wrap">
            <table className="mobile-card-table suggestions-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>范围</th>
                  <th>动作</th>
                  <th>置信度</th>
                  <th>状态</th>
                  <th>原因</th>
                </tr>
              </thead>
              <tbody>
                {!suggestions.length ? (
                  <tr><td colSpan={6} className="empty-state-small">暂无建议</td></tr>
                ) : null}
                {suggestions.slice(0, 12).map((raw, index) => {
                  const item = asRecord(raw);
                  const status = pickString(item, ["status"], "--");
                  return (
                    <tr key={`${pickString(item, ["suggestion_id"], String(index))}-${index}`}>
                      <td>{formatTime(pick(item, ["created_at"]))}</td>
                      <td>{translateScopeLabel(pickString(item, ["scope_type"], "--"), pickString(item, ["scope_key"], "--"))}</td>
                      <td>{translateDisplayValue(pickString(item, ["action"], "--"))}</td>
                      <td>{formatPct(pickNumber(item, ["confidence"], 0))}</td>
                      <td><StatusPill status={status} tone={status === "proposed" ? "warn" : status === "applied" || status === "approved" ? "ok" : "mute"} /></td>
                      <td>{shortText(translateReasonText(pickString(item, ["reason"], "--")))}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </MetricCard>

        <MetricCard title="复盘与样本" className="wide-panel">
          <div className="learning-split">
            <div>
              <div className="mini-section-title">最近交易复盘</div>
              <div className="table-wrap">
                <table className="mobile-card-table reviews-table">
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>仓位</th>
                      <th>结果</th>
                      <th>盈亏</th>
                      <th>摘要</th>
                    </tr>
                  </thead>
                  <tbody>
                    {!reviews.length ? <tr><td colSpan={5} className="empty-state-small">暂无复盘</td></tr> : null}
                    {reviews.slice(0, 6).map((raw, index) => {
                      const item = asRecord(raw);
                      return (
                        <tr key={`${pickString(item, ["review_id"], String(index))}-${index}`}>
                          <td>{formatTime(pick(item, ["created_at"]))}</td>
                          <td>{pickString(item, ["position_id", "trade_id"], "--")}</td>
                          <td>{translateDisplayValue(pickString(item, ["outcome_label"], "--"))}</td>
                          <td>{formatDecimal(pickNumber(item, ["pnl"], 0), 2)}</td>
                          <td>{shortText(pickString(item, ["summary_text"], "--"))}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
            <div>
              <div className="mini-section-title">自主学习样本</div>
              <div className="table-wrap">
                <table className="mobile-card-table samples-table">
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>类型</th>
                      <th>标签</th>
                      <th>完整性</th>
                      <th>权重</th>
                    </tr>
                  </thead>
                  <tbody>
                    {!samples.length ? <tr><td colSpan={5} className="empty-state-small">暂无样本</td></tr> : null}
                    {samples.slice(0, 6).map((raw, index) => {
                      const item = asRecord(raw);
                      return (
                        <tr key={`${pickString(item, ["sample_id"], String(index))}-${index}`}>
                          <td>{formatTime(pick(item, ["event_ts", "created_at"]))}</td>
                          <td>{translateDisplayValue(pickString(item, ["sample_type"], "--"))}</td>
                          <td>{translateDisplayValue(pickString(item, ["label_status"], "--"))}</td>
                          <td>{translateDisplayValue(pickString(item, ["integrity"], "--"))}</td>
                          <td>{formatDecimal(pickNumber(item, ["train_weight"], 0), 3)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </MetricCard>

        <MetricCard title="应用效果与生命周期" className="wide-panel">
          <div className="learning-split">
            <div>
              <div className="mini-section-title">最近应用</div>
              <div className="table-wrap">
                <table className="mobile-card-table applications-table">
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>范围</th>
                      <th>动作</th>
                      <th>状态</th>
                      <th>效果</th>
                    </tr>
                  </thead>
                  <tbody>
                    {!applications.length ? <tr><td colSpan={5} className="empty-state-small">暂无应用记录</td></tr> : null}
                    {applications.slice(0, 6).map((raw, index) => {
                      const item = asRecord(raw);
                      return (
                        <tr key={`${pickString(item, ["application_id"], String(index))}-${index}`}>
                          <td>{formatTime(pick(item, ["created_at", "cycle_ts"]))}</td>
                          <td>{translateScopeLabel(pickString(item, ["scope_type"], "--"), pickString(item, ["scope_key"], "--"))}</td>
                          <td>{translateDisplayValue(pickString(item, ["action"], "--"))}</td>
                          <td>{translateDisplayValue(pickString(item, ["status"], "--"))}</td>
                          <td>{formatDecimal(pickNumber(item, ["delta_avg_reward"], 0), 3)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
            <div>
              <div className="mini-section-title">生命周期</div>
              <div className="table-wrap">
                <table className="mobile-card-table lifecycle-table">
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>事件</th>
                      <th>因子</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {!lifecycle.length ? <tr><td colSpan={4} className="empty-state-small">暂无生命周期事件</td></tr> : null}
                    {lifecycle.slice(0, 8).map((raw, index) => {
                      const item = asRecord(raw);
                      return (
                        <tr key={`${pickString(item, ["id"], String(index))}-${index}`}>
                          <td>{formatTime(pick(item, ["ts", "timestamp"]))}</td>
                          <td>{translateDisplayValue(pickString(item, ["event"], "--"))}</td>
                          <td>{pickString(item, ["factor"], "--")}</td>
                          <td>{translateDisplayValue(pickString(item, ["status"], "--"))}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </MetricCard>

        {hasError ? (
          <MetricCard title="学习接口异常" className="wide-panel">
            <ul className="error-list">
              {summaryQuery.isError ? <li>summary：{summaryQuery.error instanceof Error ? summaryQuery.error.message : "请求失败"}</li> : null}
              {suggestionsQuery.isError ? <li>suggestions：{suggestionsQuery.error instanceof Error ? suggestionsQuery.error.message : "请求失败"}</li> : null}
              {applicationsQuery.isError ? <li>applications：{applicationsQuery.error instanceof Error ? applicationsQuery.error.message : "请求失败"}</li> : null}
              {metaReportQuery.isError ? <li>meta-lightgbm：{metaReportQuery.error instanceof Error ? metaReportQuery.error.message : "请求失败"}</li> : null}
            </ul>
          </MetricCard>
        ) : null}
      </div>
    </section>
  );
}
