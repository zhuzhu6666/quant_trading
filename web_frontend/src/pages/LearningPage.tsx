import { useQuery } from "@tanstack/react-query";
import { BrainCircuit, FileCheck2, GitBranch, GraduationCap, Layers3 } from "lucide-react";
import { MetricCard } from "@/components/Card";
import { CompactMetric as LearningMiniMetric, Field, StatTile, numberTone, toneFromStatus } from "@/components/DashboardBits";
import { QueryErrorList } from "@/components/QueryErrorList";
import { StatusPill } from "@/components/StatusPill";
import {
  getAutonomousLearningSamples,
  getLearningApplications,
  getLearningLifecycle,
  getLearningReviews,
  getLearningSummary,
  getLearningSuggestions,
} from "@/api/client";
import { asRecord, pick, pickArray, pickBoolean, pickNumber, pickRecord, pickString } from "@/lib/compat";
import { translateDisplayValue, translateReasonText, translateScopeLabel } from "@/lib/display";
import { formatDecimal, formatTime } from "@/lib/format";
import { useBackendReadinessQuery } from "@/hooks/useCoreQueries";
import { factBoundTone, factIsKnown, readFact } from "@/api/fact";

function countFrom(record: unknown, key: string): number {
  return pickNumber(record, [key], 0);
}

function formatPct(value: number): string {
  return `${formatDecimal(value * 100, 1)}%`;
}

function shortText(value: unknown, fallback = ""): string {
  const text = String(value || "").trim();
  if (!text) return fallback;
  return text.length > 96 ? `${text.slice(0, 96)}...` : text;
}

function fullText(value: unknown, fallback = ""): string {
  const text = String(value || "").trim();
  return text || fallback;
}

function statusTone(status: string): "ok" | "warn" | "bad" | "mute" {
  const normalized = status.toLowerCase();
  if (["applied", "approved", "ok", "healthy", "completed", "accepted"].includes(normalized)) return "ok";
  if (["proposed", "pending", "watch", "queued", "review"].includes(normalized)) return "warn";
  if (["failed", "rejected", "rolled_back", "error", "blocked"].includes(normalized)) return "bad";
  return "mute";
}

function shortId(value: string): string {
  if (!value || value === "") return "";
  return value.length > 18 ? `${value.slice(0, 10)}...${value.slice(-4)}` : value;
}

export function LearningPage({ embedded = false }: { embedded?: boolean }) {
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
  const readinessQuery = useBackendReadinessQuery(30_000);

  const summary = asRecord(summaryQuery.data);
  const suggestionCounts = asRecord(pick(summary, ["suggestions"]));
  const reviewCounts = asRecord(pick(summary, ["reviews"]));
  const candidateCounts = asRecord(pick(summary, ["parameter_template_candidates"]));
  const recommendationCounts = asRecord(pick(summary, ["parameter_template_recommendations"]));
  const latestReview = asRecord(pick(summary, ["latest_review"]));
  const todo = asRecord(pick(summary, ["parameter_template_todo"]));
  const overview = asRecord(pick(summary, ["parameter_template_overview"]));
  const readiness = asRecord(readinessQuery.data);
  const readinessFact = readFact(readinessQuery.data, "ops.backend-readiness.v2");
  const readinessRequestFailed = readinessQuery.isError || readinessQuery.isRefetchError;
  const readinessKnown = factIsKnown(readinessFact, readinessRequestFailed);
  const governance = asRecord(pick(readiness, ["governance"]));
  const factorData = asRecord(pick(readiness, ["factor_data"]));
  const factorState = asRecord(pick(factorData, ["state"]));
  const factorHealth = asRecord(pick(factorState, ["factor_health_by_status"]));
  const highLoad = asRecord(pick(readiness, ["high_load"]));
  const effectQuality = asRecord(pick(readiness, ["learning_effect_quality"]));
  const effectStatuses = asRecord(pick(effectQuality, ["status_counts"]));
  const effectReasons = asRecord(pick(effectQuality, ["reason_counts"]));
  const effectSlo = asRecord(pick(effectQuality, ["slo"]));
  const effectPrior = asRecord(pick(effectQuality, ["experience_prior"]));
  const aweMutationCoverage = asRecord(pick(effectQuality, ["awe_mutation_coverage"]));
  const runtimeFactorBudget = asRecord(pick(readiness, ["runtime_factor_budget"]));
  const effectActive = pickNumber(effectQuality, ["active_count"], 0);
  const effectClosure = pickNumber(effectQuality, ["closure_ratio"], 0);
  const retryCandidates = pickNumber(effectQuality, ["retry_candidate_count"], 0);
  const confoundedEffects = pickNumber(effectReasons, ["confounded_by_concurrent_application"], 0);
  const aweCoverageEnforced = pickNumber(aweMutationCoverage, ["enforced_from"], 0) > 0;

  const suggestions = pickArray(suggestionsQuery.data, ["items"]);
  const applications = pickArray(applicationsQuery.data, ["items"]);
  const lifecycle = pickArray(lifecycleQuery.data, ["items"]);
  const reviews = pickArray(reviewsQuery.data, ["items"]);
  const samples = pickArray(samplesQuery.data, ["items"]);
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
  const automaticExecution = pickBoolean(governance, ["automatic_execution_enabled"], false);
  const autonomyMode = pickString(governance, ["autonomy_mode"], "");
  const latestFactorUpdate = asRecord(pick(factorData, ["last_enrichment"]));
  const highLoadProfile = pickString(highLoad, ["profile"], "");

  const learningQueries = [
    summaryQuery,
    suggestionsQuery,
    applicationsQuery,
    lifecycleQuery,
    reviewsQuery,
    samplesQuery,
    readinessQuery,
  ];
  const hasError = learningQueries.some((query) => query.isError || query.isRefetchError);
  const isRefreshing = learningQueries.some((query) => query.isFetching);
  const learningFactsKnown = readinessKnown && [
    factIsKnown(readFact(summaryQuery.data, "learning.summary.v2"), summaryQuery.isError || summaryQuery.isRefetchError),
    factIsKnown(readFact(suggestionsQuery.data, "learning.suggestions.v2"), suggestionsQuery.isError || suggestionsQuery.isRefetchError),
    factIsKnown(readFact(applicationsQuery.data, "learning.applications.v2"), applicationsQuery.isError || applicationsQuery.isRefetchError),
    factIsKnown(readFact(lifecycleQuery.data, "learning.lifecycle.v2"), lifecycleQuery.isError || lifecycleQuery.isRefetchError),
    factIsKnown(readFact(reviewsQuery.data, "learning.reviews.v2"), reviewsQuery.isError || reviewsQuery.isRefetchError),
    factIsKnown(readFact(samplesQuery.data, "learning.autonomous-samples.v2"), samplesQuery.isError || samplesQuery.isRefetchError),
  ].every(Boolean);

  return (
    <section className="dashboard learning-dashboard">
      {!embedded ? <div className="dashboard-header">
        <div>
          <div className="eyebrow">学习闭环</div>
          <h1>学习与治理</h1>
          <p>复盘、样本、策略建议、参数治理和只观察模型统一在这里查看。</p>
        </div>
        <div className="header-status">
          <StatusPill status={readinessKnown ? (automaticExecution ? "自动应用已开" : "人工审核") : "治理状态待确认"} tone={factBoundTone(readinessFact, automaticExecution ? "warn" : "ok", readinessRequestFailed)} />
          <StatusPill status={`模式 ${translateDisplayValue(autonomyMode)}`} tone="mute" />
          <StatusPill status={hasError ? "接口异常" : learningFactsKnown ? "学习链路在线" : isRefreshing ? "部分数据更新中" : "部分数据待确认"} tone={hasError ? "bad" : learningFactsKnown ? "ok" : "warn"} />
        </div>
      </div> : null}

      {!embedded ? <div className="stat-grid">
        <StatTile
          icon={BrainCircuit}
          label="待治理建议（前端汇总）"
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
      </div> : null}

      <div className="dashboard-grid">
        <MetricCard title="学习结果：证据 → 候选 → 应用" className="wide-panel learning-control-panel learning-result-panel">
          <div className="learning-mini-grid">
            <LearningMiniMetric label="治理待办" value={formatDecimal(proposed + pendingCandidates, 0)} detail={`建议 ${proposed} · 候选 ${pendingCandidates}`} tone={proposed + pendingCandidates > 0 ? "warn" : "ok"} />
            <LearningMiniMetric label="建议已应用" value={formatDecimal(applied, 0)} detail={`批准 ${approved} · 回滚 ${rolledBack}`} tone={rolledBack > 0 ? "warn" : applied > 0 ? "ok" : "mute"} />
            <LearningMiniMetric label="样本池" value={formatDecimal(sampleCount, 0)} detail={`复盘 ${reviews.length} · 应用 ${applicationsCount}`} tone={sampleCount > 0 ? "ok" : "mute"} />
            <LearningMiniMetric label="因子健康" value={`${healthyFactors}/${totalFactors || ""}`} detail={`观察 ${watchFactors}`} tone={watchFactors > 0 ? "warn" : healthyFactors > 0 ? "ok" : "mute"} />
            <LearningMiniMetric label="闭环率" value={formatPct(effectClosure)} detail={`终态 ${pickNumber(effectQuality, ["terminal_count"], 0)}`} tone={effectClosure >= 0.7 ? "ok" : "warn"} />
            <LearningMiniMetric label="开放窗口" value={formatDecimal(effectActive, 0)} detail={`观察 ${pickNumber(effectStatuses, ["observing"], 0)} · 混合 ${pickNumber(effectStatuses, ["mixed"], 0)}`} tone={effectActive ? "warn" : "ok"} />
            <LearningMiniMetric label="证据不足" value={formatDecimal(pickNumber(effectStatuses, ["inconclusive"], 0), 0)} detail={`受控重试候选 ${retryCandidates}`} tone={retryCandidates ? "warn" : "mute"} />
            <LearningMiniMetric label="效果评估" value={translateDisplayValue(pickString(effectSlo, ["status"], pickString(effectQuality, ["status"], "")))} detail="只读质量检查" tone={toneFromStatus(pickString(effectSlo, ["status"], ""))} />
          </div>
          <div className="learning-control-grid">
            <section className="learning-control-section">
              <div className="learning-section-head">
                <h3>当前治理结论</h3>
                <StatusPill status={automaticExecution ? "自动应用" : "人工审核"} tone={automaticExecution ? "warn" : "ok"} />
              </div>
              <div className="learning-note">{fullText(pickString(summary, ["parameter_template_ops_summary"], pickString(overview, ["headline"], "")), "")}</div>
              <div className="learning-chip-row">
                <span className="data-badge">推荐 {countFrom(recommendationCounts, "total")}</span>
                <span className="data-badge">在线 {countFrom(recommendationCounts, "online_light")}</span>
                <span className="data-badge">离线 {countFrom(recommendationCounts, "offline_deep")}</span>
              </div>
            </section>

            <section className="learning-control-section">
              <div className="learning-section-head">
                <h3>最近复盘</h3>
                <StatusPill status={latestReview.review_id ? translateDisplayValue(pickString(latestReview, ["outcome_label"], "")) : "暂无"} tone={latestReview.review_id ? numberTone(pickNumber(latestReview, ["pnl"], 0)) : "mute"} />
              </div>
              <div className="learning-note">
                {latestReview.review_id ? `${formatDecimal(pickNumber(latestReview, ["pnl"], 0), 2)} · ${fullText(pickString(latestReview, ["summary_text", "review_summary"], ""))}` : fullText(pickString(todo, ["title", "summary"], "暂无复盘任务"))}
              </div>
            </section>

            <section className="learning-control-section">
              <div className="learning-section-head">
                <h3>训练与因子数据</h3>
                <StatusPill status={translateDisplayValue(highLoadProfile)} tone={toneFromStatus(highLoadProfile)} />
              </div>
              <div className="field-list learning-compact-fields">
                <Field label="盘外训练任务" value={translateDisplayValue(highLoadProfile)} tone={toneFromStatus(highLoadProfile)} />
                <Field label="因子更新" value={formatTime(pick(latestFactorUpdate, ["updated_at", "ts"]))} tone={pickBoolean(latestFactorUpdate, ["ok"], true) ? "ok" : "bad"} />
              </div>
            </section>
          </div>

          <details className="detail-disclosure learning-quality-disclosure">
            <summary>展开学习质量明细（并发、强化、经验、权重和生产因子）</summary>
            <div className="learning-mini-grid">
              <LearningMiniMetric label="并发积压" value={formatDecimal(confoundedEffects, 0)} detail="目标为 0" tone={confoundedEffects ? "warn" : "ok"} />
              <LearningMiniMetric label="已强化" value={formatDecimal(pickNumber(effectStatuses, ["reinforced"], 0), 0)} detail={`无效 ${pickNumber(effectStatuses, ["ineffective"], 0)}`} tone={pickNumber(effectStatuses, ["reinforced"], 0) ? "ok" : "mute"} />
              <LearningMiniMetric label="经验先验" value={formatDecimal(pickNumber(effectPrior, ["eligible_count"], 0), 0)} detail={`有界因子 ${pickNumber(effectPrior, ["bounded_factor_count"], 0)}`} tone={pickNumber(effectPrior, ["eligible_count"], 0) ? "ok" : "warn"} />
              <LearningMiniMetric label="权重自适应记录" value={aweCoverageEnforced ? formatPct(pickNumber(aweMutationCoverage, ["coverage_ratio"], 0)) : "待新周期"} detail={`历史缺口 ${pickNumber(aweMutationCoverage, ["legacy_missing_count"], 0)}`} tone={aweCoverageEnforced ? toneFromStatus(pickString(aweMutationCoverage, ["status"], "")) : "mute"} />
              <LearningMiniMetric label="生产因子" value={formatDecimal(pickNumber(runtimeFactorBudget, ["selected_count"], 0), 0)} detail={`冷尾部 ${pickNumber(runtimeFactorBudget, ["budget_excluded_count"], 0)}`} tone={pickBoolean(runtimeFactorBudget, ["ok"], false) ? "ok" : "warn"} />
            </div>
          </details>
          <div className="learning-note">只有出现新的复盘证据，并且期间没有新的配置应用时，系统才会重新评估；看板不会自动改权重、参数或智能体权限。</div>
        </MetricCard>

        <MetricCard title="策略建议队列" className="learning-side-panel">
          {!suggestions.length ? (
            <div className="empty-state-small">暂无建议</div>
          ) : (
            <div className="learning-card-list">
              {suggestions.slice(0, 8).map((raw, index) => {
                const item = asRecord(raw);
                const status = pickString(item, ["status"], "");
                return (
                  <article className="learning-list-item" key={`${pickString(item, ["suggestion_id"], String(index))}-${index}`}>
                    <div className="learning-list-main">
                      <div>
                        <strong>{translateDisplayValue(pickString(item, ["action"], ""))}</strong>
                        <span>{translateScopeLabel(pickString(item, ["scope_type"], ""), pickString(item, ["scope_key"], ""))}</span>
                      </div>
                      <StatusPill status={status} tone={statusTone(status)} />
                    </div>
                    <div className="learning-list-meta">
                      <span><BrainCircuit size={13} /> {formatPct(pickNumber(item, ["confidence"], 0))}</span>
                      <span>{formatTime(pick(item, ["created_at"]))}</span>
                    </div>
                    <p>{shortText(translateReasonText(pickString(item, ["reason"], "")))}</p>
                  </article>
                );
              })}
            </div>
          )}
        </MetricCard>

        <MetricCard title="复盘样本流" className="learning-side-panel">
          <div className="learning-stream-grid">
            <section>
              <div className="mini-section-title"><FileCheck2 size={14} /> 最近交易复盘</div>
              <div className="learning-event-list">
                {!reviews.length ? <div className="empty-state-small">暂无复盘</div> : null}
                {reviews.slice(0, 5).map((raw, index) => {
                  const item = asRecord(raw);
                  const pnl = pickNumber(item, ["pnl"], 0);
                  return (
                    <div className="learning-event-row" key={`${pickString(item, ["review_id"], String(index))}-${index}`}>
                      <div>
                        <strong>{translateDisplayValue(pickString(item, ["outcome_label"], ""))}</strong>
                        <span>{shortId(pickString(item, ["position_id", "trade_id"], ""))} · {formatTime(pick(item, ["created_at"]))}</span>
                      </div>
                      <StatusPill status={formatDecimal(pnl, 2)} tone={numberTone(pnl)} />
                    </div>
                  );
                })}
              </div>
            </section>

            <section>
              <div className="mini-section-title"><GraduationCap size={14} /> 自主学习样本</div>
              <div className="learning-event-list">
                {!samples.length ? <div className="empty-state-small">暂无样本</div> : null}
                {samples.slice(0, 5).map((raw, index) => {
                  const item = asRecord(raw);
                  return (
                    <div className="learning-event-row" key={`${pickString(item, ["sample_id"], String(index))}-${index}`}>
                      <div>
                        <strong>{translateDisplayValue(pickString(item, ["sample_type"], ""))}</strong>
                        <span>{translateDisplayValue(pickString(item, ["label_status"], ""))} · {formatTime(pick(item, ["event_ts", "created_at"]))}</span>
                      </div>
                      <span className="learning-weight">{formatDecimal(pickNumber(item, ["train_weight"], 0), 3)}</span>
                    </div>
                  );
                })}
              </div>
            </section>
          </div>
        </MetricCard>

        <MetricCard title="应用与生命周期" className="wide-panel">
          <div className="learning-stream-grid">
            <section>
              <div className="mini-section-title"><GitBranch size={14} /> 最近应用</div>
              <div className="learning-event-list">
                {!applications.length ? <div className="empty-state-small">暂无应用记录</div> : null}
                {applications.slice(0, 5).map((raw, index) => {
                  const item = asRecord(raw);
                  const status = pickString(item, ["status"], "");
                  return (
                    <div className="learning-event-row" key={`${pickString(item, ["application_id"], String(index))}-${index}`}>
                      <div>
                        <strong>{translateDisplayValue(pickString(item, ["action"], ""))}</strong>
                        <span>{translateScopeLabel(pickString(item, ["scope_type"], ""), pickString(item, ["scope_key"], ""))}</span>
                      </div>
                      <StatusPill status={status} tone={statusTone(status)} />
                    </div>
                  );
                })}
              </div>
            </section>

            <section>
              <div className="mini-section-title"><Layers3 size={14} /> 生命周期</div>
              <div className="learning-event-list">
                {!lifecycle.length ? <div className="empty-state-small">暂无生命周期事件</div> : null}
                {lifecycle.slice(0, 6).map((raw, index) => {
                  const item = asRecord(raw);
                  const status = pickString(item, ["status"], "");
                  return (
                    <div className="learning-event-row" key={`${pickString(item, ["id"], String(index))}-${index}`}>
                      <div>
                        <strong>{translateDisplayValue(pickString(item, ["event"], ""))}</strong>
                        <span>{pickString(item, ["factor"], "")} · {formatTime(pick(item, ["ts", "timestamp"]))}</span>
                      </div>
                      <StatusPill status={status} tone={statusTone(status)} />
                    </div>
                  );
                })}
              </div>
            </section>
          </div>
        </MetricCard>

        {hasError ? (
          <MetricCard title="学习接口异常" className="wide-panel">
            <QueryErrorList queries={[
              { label: "summary", query: summaryQuery },
              { label: "suggestions", query: suggestionsQuery },
              { label: "applications", query: applicationsQuery },
              { label: "lifecycle", query: lifecycleQuery },
              { label: "reviews", query: reviewsQuery },
              { label: "samples", query: samplesQuery },
              { label: "backend-readiness", query: readinessQuery },
            ]} />
          </MetricCard>
        ) : null}
      </div>
    </section>
  );
}
