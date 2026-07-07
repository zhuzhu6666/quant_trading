import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BrainCircuit, Database, GitBranch, ListChecks, Play, RefreshCw, ShieldCheck, Sparkles, Workflow } from "lucide-react";
import {
  evaluateLiveAutonomyUnlock,
  evaluateBrainLiveReadyGuardrail,
  getBackendReadiness,
  getAutonomyProposals,
  getBrainActionPlanEvals,
  getBrainActionPlans,
  getBrainLiveReadyGuardrails,
  getBrainLowImpactExecutions,
  getBrainGovernanceCandidateReviews,
  getBrainMediumImpactGovernance,
  getBrainMemory,
  getBrainState,
  getLiveAutonomyStatus,
  materializeBrainMediumImpactGovernance,
  refreshAutonomyProposals,
  reviewAutonomyProposal,
  reviewBrainGovernanceCandidates,
  runBrainLowImpactExecution,
  revokeLiveAutonomy,
  tightenBrainLiveReadyGuardrail,
  unlockLiveAutonomy,
} from "@/api/client";
import { MetricCard } from "@/components/Card";
import { Field, StatTile, toneFromStatus, type Tone } from "@/components/DashboardBits";
import { JsonBlock } from "@/components/JsonBlock";
import { StatusPill } from "@/components/StatusPill";
import { formatDecimal } from "@/lib/format";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function pick(value: unknown, path: string[]): unknown {
  let current: unknown = value;
  for (const key of path) {
    const parts = key.split(".");
    for (const part of parts) {
      const record = asRecord(current);
      current = record[part];
    }
  }
  return current;
}

function pickString(value: unknown, path: string[], fallback = "--"): string {
  const raw = pick(value, path);
  if (raw === null || raw === undefined || raw === "") return fallback;
  return String(raw);
}

function pickNumber(value: unknown, path: string[], fallback = 0): number {
  const raw = pick(value, path);
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function pickBoolean(value: unknown, path: string[], fallback = false): boolean {
  const raw = pick(value, path);
  return typeof raw === "boolean" ? raw : fallback;
}

function pickArray(value: unknown, path: string[]): unknown[] {
  const raw = pick(value, path);
  return Array.isArray(raw) ? raw : [];
}

function formatTime(raw: unknown): string {
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) return "--";
  return new Date(value * 1000).toLocaleString();
}

function scorePct(raw: number): number {
  if (!Number.isFinite(raw)) return 0;
  return raw <= 1 ? raw * 100 : raw;
}

function boolTone(value: boolean): Tone {
  return value ? "ok" : "warn";
}

function riskTone(value: string): Tone {
  const normalized = value.toLowerCase();
  if (["high", "critical", "bad"].includes(normalized)) return "bad";
  if (["medium", "warn"].includes(normalized)) return "warn";
  if (["low", "ok"].includes(normalized)) return "ok";
  return "mute";
}

function displayStage(value: string): string {
  const normalized = value.toLowerCase();
  const labels: Record<string, string> = {
    phase5_live_ready_guardrails: "实盘护栏",
    v16_phase1_read_only_brain: "只读认知",
    v16_phase2_shadow_brain: "影子规划",
    v16_phase2_shadow_brain_eval: "后验评价",
    v16_phase3_low_impact_autonomous_brain: "低影响执行",
    v16_phase4_medium_impact_governance: "治理候选",
    v16_phase5_live_ready_guardrails: "实盘护栏",
  };
  return labels[normalized] || value.replace(/^v\d+_/, "").replaceAll("_", " ");
}

function displayAction(value: string): string {
  const labels: Record<string, string> = {
    shadow_supervisor_template_review: "Supervisor 模板复核",
    shadow_factor_weight_review: "因子权重复核",
    shadow_context_policy_review: "Context Policy 复核",
    shadow_parameter_template_review: "参数模板复核",
    run_replay_job: "只读回放",
    update_weight: "权重候选",
    switch_parameter_template: "参数模板候选",
    enable_context_policy: "Context Policy 候选",
    switch_position_supervisor_template: "Supervisor 模板候选",
    observe: "观察",
    tighten_to_no_new_risk: "收紧到不增风险",
    tighten_to_only_close: "收紧到仅平仓",
    freeze_autonomy: "冻结自治",
  };
  return labels[value] || value.replaceAll("_", " ");
}

function displayValue(value: string): string {
  const normalized = value.toLowerCase();
  const labels: Record<string, string> = {
    aligned: "已对齐",
    allow: "允许",
    active: "活跃",
    block: "阻断",
    blocked: "已阻断",
    bridge_ready: "可桥接",
    candidate_materialized: "候选已生成",
    caution: "谨慎",
    divergent: "有偏差",
    frozen: "冻结",
    governance_ready: "治理就绪",
    conflict_detected: "冲突待审",
    degraded: "退化",
    fresh: "新鲜",
    high: "高",
    high_unresolved_conflicts: "高危冲突",
    live_ready: "实盘就绪",
    "live-ready": "实盘就绪",
    live_autonomous: "实盘自治",
    live_candidate: "实盘候选",
    locked: "已锁定",
    low: "低",
    manual: "人工模式",
    medium: "中",
    medium_impact: "中影响",
    missing: "缺失",
    missing_timestamp: "缺时间戳",
    negative: "负面",
    needs_evidence: "缺证据",
    neutral: "中性",
    not_bridge_compatible: "暂不可桥接",
    no_new_risk: "不增风险",
    none_shadow_only: "仅影子无影响",
    normal: "正常",
    observation_only: "仅观察",
    ok: "正常",
    only_close: "仅平仓",
    pass: "通过",
    positive: "正面",
    posterior: "后验",
    proposal_registry: "提案总线",
    request_review: "请求审查",
    request_replay: "请求回放",
    ready: "就绪",
    reject: "拒绝",
    revoked: "已撤销",
    reviewed: "已审查",
    shadow: "影子",
    shadow_recorded: "影子已记录",
    stale: "过期",
    stale_evidence: "证据过期",
    submit_governance: "提交治理",
    submitted: "已提交",
    submitted_to_policy_suggestion: "已提交建议队列",
    supportive: "支持",
    tighten_incident: "收紧事故模式",
    unlock_ready: "可解锁",
    unlocked: "已解锁",
    suggestion_materialized: "建议已生成",
    unknown: "未知",
    warn: "注意",
  };
  return labels[normalized] || value.replace(/^v\d+_/, "").replace(/\.v\d+$/, "").replaceAll("_", " ");
}

function displayContract(value: string): string {
  const normalized = value.toLowerCase();
  const labels: Record<string, string> = {
    backend_readiness: "后端就绪契约",
    v15_readiness_contract: "运行中枢就绪契约",
    v16_readiness_contract: "自治大脑就绪契约",
    brain_state_snapshot: "大脑状态快照",
    brain_memory_retrieval: "记忆检索契约",
    brain_action_plan_run: "影子计划契约",
    brain_action_plan_eval_run: "后验评价契约",
    brain_low_impact_execution_run: "低影响执行契约",
    brain_medium_impact_governance_run: "治理候选契约",
    brain_governance_candidate_review_list: "候选审查契约",
    brain_governance_candidate_review_run: "候选审查契约",
    brain_live_ready_guardrail: "实盘护栏契约",
    proposal_registry_list: "提案总线契约",
    proposal_registry_status: "提案总线状态",
    live_autonomy_status: "实盘自治契约",
  };
  const base = normalized.replace(/\.\d+$/, "").replace(/\.v\d+$/, "");
  return labels[base] || value.replace(/^v\d+_/, "").replace(/\.v\d+$/, "").replaceAll("_", " ");
}

function CompactFacts({ facts }: { facts: Array<{ label: string; value: string; tone?: Tone }> }) {
  return (
    <div className="brain-compact-facts">
      {facts.map((fact) => (
        <span className={`brain-compact-fact brain-compact-${fact.tone || "mute"}`} key={`${fact.label}-${fact.value}`}>
          <b>{fact.label}</b>
          {fact.value}
        </span>
      ))}
    </div>
  );
}

function CompactMetric({ label, value, detail, tone = "mute" }: { label: string; value: string; detail?: string; tone?: Tone }) {
  return (
    <div className={`v15-compact-metric v15-compact-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail ? <small>{detail}</small> : null}
    </div>
  );
}

function SectionHead({ title, status, tone = "mute" }: { title: string; status?: string; tone?: Tone }) {
  return (
    <div className="v15-section-head">
      <h3>{title}</h3>
      {status ? <StatusPill status={status} tone={tone} /> : null}
    </div>
  );
}

function MemoryList({ items, empty }: { items: unknown[]; empty: string }) {
  if (!items.length) return <div className="empty-state-small">{empty}</div>;
  return (
    <div className="v15-list">
      {items.slice(0, 8).map((raw, index) => {
        const item = asRecord(raw);
        const source = pickString(item, ["source_table"], "memory");
        const sourceId = pickString(item, ["source_id"], "");
        const polarity = pickString(item, ["polarity"], "neutral");
        const summary = pickString(item, ["text_summary"], source);
        const meta = `${source} · evidence ${formatDecimal(pickNumber(item, ["evidence_score"], 0), 2)} · similarity ${formatDecimal(pickNumber(item, ["similarity_score"], 0), 2)}`;
        return (
          <div className="v15-list-row brain-memory-row" key={`${source}-${sourceId}-${index}`}>
            <div>
              <strong title={summary}>{summary}</strong>
              <span title={meta}>{meta}</span>
            </div>
            <StatusPill status={displayValue(polarity)} tone={polarity === "negative" ? "bad" : polarity === "positive" ? "ok" : "mute"} />
          </div>
        );
      })}
    </div>
  );
}

function HypothesisList({ items }: { items: unknown[] }) {
  if (!items.length) return <div className="empty-state-small">暂无 hypothesis</div>;
  return (
    <div className="brain-hypothesis-list">
      {items.map((raw, index) => {
        const item = asRecord(raw);
        const risk = pickString(item, ["risk_class"], "unknown");
        return (
          <article className="brain-hypothesis" key={`${pickString(item, ["hypothesis_id"], "hyp")}-${index}`}>
            <div className="brain-hypothesis-head">
              <div>
                <strong>{pickString(item, ["scope"], "scope")}</strong>
                <span>{pickString(item, ["claim"], "--")}</span>
              </div>
              <StatusPill status={displayValue(risk)} tone={riskTone(risk)} />
            </div>
            <div className="v15-mini-grid v15-mini-grid-tight">
              <CompactMetric label="置信度" value={`${formatDecimal(scorePct(pickNumber(item, ["confidence"], 0)), 1)}%`} tone="mute" />
              <CompactMetric label="证据分" value={`${formatDecimal(scorePct(pickNumber(item, ["evidence_score"], 0)), 1)}%`} tone={pickNumber(item, ["evidence_score"], 0) >= 0.5 ? "ok" : "warn"} />
              <CompactMetric label="动作范围" value={pickString(item, ["action_scope"], "observe_only")} tone="warn" />
            </div>
            <div className="brain-ref-row">
              <span>证据 {Object.keys(asRecord(pick(item, ["evidence_refs"]))).length}</span>
              <span>反证 {Object.keys(asRecord(pick(item, ["counter_evidence_refs"]))).length}</span>
            </div>
          </article>
        );
      })}
    </div>
  );
}

function ActionPlanList({ items }: { items: unknown[] }) {
  if (!items.length) return <div className="empty-state-small">暂无影子计划</div>;
  return (
    <div className="brain-action-plan-list">
      {items.slice(0, 8).map((raw, index) => {
        const item = asRecord(raw);
        const scope = asRecord(pick(item, ["scope"]));
        const status = pickString(item, ["status"], "unknown");
        const verdict = pickString(item, ["critic_verdict"], "unknown");
        const risk = pickString(item, ["risk_class"], "unknown");
        const requiredServices = pickArray(item, ["required_services"]).map(String);
        return (
          <article className="brain-action-plan brain-action-plan-compact" key={`${pickString(item, ["plan_id"], "plan")}-${index}`}>
            <div className="brain-hypothesis-head">
              <div>
                <strong>{displayAction(pickString(item, ["action_type"], "action_plan"))}</strong>
                <span>
                  {pickString(scope, ["scope_type"], "scope")} · {pickString(scope, ["scope_key"], "--")}
                </span>
              </div>
              <StatusPill status={displayValue(status)} tone={status === "shadow_recorded" ? "ok" : "warn"} />
            </div>
            <CompactFacts facts={[
              { label: "Critic", value: displayValue(verdict), tone: verdict === "pass" ? "ok" : verdict === "reject" ? "bad" : "warn" },
              { label: "风险", value: displayValue(risk), tone: riskTone(risk) },
              { label: "影响", value: displayValue(pickString(item, ["max_impact"], "none_shadow_only")), tone: "ok" },
            ]} />
            <div className="brain-plan-services">
              {requiredServices.slice(0, 4).map((service) => (
                <span key={service}>{service}</span>
              ))}
            </div>
          </article>
        );
      })}
    </div>
  );
}

function EvaluationList({ items }: { items: unknown[] }) {
  if (!items.length) return <div className="empty-state-small">暂无后验评价</div>;
  return (
    <div className="brain-action-plan-list">
      {items.slice(0, 8).map((raw, index) => {
        const item = asRecord(raw);
        const comparison = asRecord(pick(item, ["comparison"]));
        const presence = asRecord(pick(comparison, ["source_presence"]));
        const verdict = pickString(item, ["comparison_verdict"], "unknown");
        const coverage = scorePct(pickNumber(item, ["coverage_score"], 0));
        return (
          <article className="brain-action-plan brain-action-plan-compact" key={`${pickString(item, ["eval_id"], "eval")}-${index}`}>
            <div className="brain-hypothesis-head">
              <div>
                <strong>{displayAction(pickString(item, ["action_type"], "action_plan_eval"))}</strong>
                <span>{pickString(item, ["scope_type"], "scope")} · {pickString(item, ["status"], "needs_evidence")}</span>
              </div>
              <StatusPill status={displayValue(verdict)} tone={verdict === "supportive" ? "ok" : verdict === "caution" ? "warn" : "mute"} />
            </div>
            <CompactFacts facts={[
              { label: "覆盖", value: `${formatDecimal(coverage, 1)}%`, tone: coverage >= 50 ? "ok" : "warn" },
              { label: "Replay", value: pickBoolean(presence, ["replay_report"], false) ? "yes" : "no", tone: boolTone(pickBoolean(presence, ["replay_report"], false)) },
              { label: "Outcome", value: pickBoolean(presence, ["trade_outcome_review"], false) ? "yes" : "no", tone: boolTone(pickBoolean(presence, ["trade_outcome_review"], false)) },
              { label: "Supervisor", value: pickBoolean(presence, ["position_supervisor_trace"], false) ? "yes" : "no", tone: boolTone(pickBoolean(presence, ["position_supervisor_trace"], false)) },
            ]} />
            <div className="brain-ref-row">
              <span>delta {formatDecimal(pickNumber(comparison, ["learning_effects.avg_delta_reward"], 0), 3)}</span>
              <span>avg pnl {formatDecimal(pickNumber(comparison, ["trade_outcomes.avg_pnl"], 0), 2)}</span>
              <span>agreement {formatDecimal(scorePct(pickNumber(comparison, ["replay.agreement"], 0)), 1)}%</span>
            </div>
          </article>
        );
      })}
    </div>
  );
}

function ExecutionList({ items }: { items: unknown[] }) {
  if (!items.length) return <div className="empty-state-small">暂无 low-impact execution</div>;
  return (
    <div className="brain-action-plan-list">
      {items.slice(0, 8).map((raw, index) => {
        const item = asRecord(raw);
        const status = pickString(item, ["status"], "unknown");
        const riskVerdict = asRecord(pick(item, ["risk_verdict"]));
        const result = asRecord(pick(item, ["result"]));
        const posterior = asRecord(pick(item, ["posterior_monitor"]));
        return (
          <article className="brain-action-plan brain-action-plan-compact" key={`${pickString(item, ["execution_id"], "execution")}-${index}`}>
            <div className="brain-hypothesis-head">
              <div>
                <strong>{displayAction(pickString(item, ["execution_action"], "low_impact_action"))}</strong>
                <span>{pickString(item, ["action_type"], "action_plan")} · {formatTime(pick(item, ["created_at"]))}</span>
              </div>
              <StatusPill status={displayValue(status)} tone={status.includes("blocked") ? "bad" : status.includes("downgraded") ? "warn" : "ok"} />
            </div>
            <CompactFacts facts={[
              { label: "证据", value: `${formatDecimal(scorePct(pickNumber(item, ["evidence_score"], 0)), 1)}%`, tone: "ok" },
              { label: "RiskPolicy", value: displayValue(pickBoolean(riskVerdict, ["allowed"], false) ? "allow" : "block"), tone: boolTone(pickBoolean(riskVerdict, ["allowed"], false)) },
              { label: "Critic", value: displayValue(pickString(item, ["critic_verdict"], "unknown")), tone: pickString(item, ["critic_verdict"], "") === "reject" ? "bad" : "warn" },
              { label: "后验", value: displayValue(pickString(item, ["comparison_verdict"], "unknown")), tone: pickString(item, ["comparison_verdict"], "") === "caution" ? "warn" : "ok" },
            ]} />
            <div className="brain-ref-row">
              <span>{pickString(result, ["replay_run_id"], "no replay")}</span>
              <span>bad posterior {pickBoolean(posterior, ["bad_posterior"], false) ? "yes" : "no"}</span>
              <span>{pickString(riskVerdict, ["reason"], "--")}</span>
            </div>
          </article>
        );
      })}
    </div>
  );
}

function GovernanceList({ items }: { items: unknown[] }) {
  if (!items.length) return <div className="empty-state-small">暂无治理候选</div>;
  return (
    <div className="brain-action-plan-list">
      {items.slice(0, 8).map((raw, index) => {
        const item = asRecord(raw);
        const status = pickString(item, ["status"], "unknown");
        const riskVerdict = asRecord(pick(item, ["risk_verdict"]));
        const decisionPolicy = asRecord(pick(item, ["decision_policy"]));
        return (
          <article className="brain-action-plan brain-action-plan-compact" key={`${pickString(item, ["governance_id"], "governance")}-${index}`}>
            <div className="brain-hypothesis-head">
              <div>
                <strong>{displayAction(pickString(item, ["governance_action"], "governance_action"))}</strong>
                <span>{pickString(item, ["scope_type"], "scope")} · {pickString(item, ["scope_key"], "--")}</span>
              </div>
              <StatusPill status={displayValue(status)} tone={status === "candidate_materialized" ? "ok" : status.includes("blocked") ? "warn" : "mute"} />
            </div>
            <CompactFacts facts={[
              { label: "证据", value: `${formatDecimal(scorePct(pickNumber(item, ["evidence_score"], 0)), 1)}%`, tone: "ok" },
              { label: "RiskPolicy", value: displayValue(pickBoolean(riskVerdict, ["allowed"], false) ? "allow" : "block"), tone: boolTone(pickBoolean(riskVerdict, ["allowed"], false)) },
              { label: "DecisionPolicy", value: pickBoolean(decisionPolicy, ["required"], false) ? "preview" : "n/a", tone: "mute" },
              { label: "候选", value: pickString(item, ["candidate_id"], "--"), tone: pickString(item, ["candidate_id"], "") ? "ok" : "mute" },
              { label: "建议", value: pickString(item, ["suggestion_id"], "--"), tone: pickString(item, ["suggestion_id"], "") ? "ok" : "mute" },
            ]} />
            <div className="brain-ref-row">
              <span>{displayValue(pickString(item, ["comparison_verdict"], "unknown"))}</span>
              <span>{pickString(riskVerdict, ["reason"], "--")}</span>
              <span>runtime mutation {pickBoolean(item, ["rollback_plan.runtime_mutation"], false) ? "yes" : "no"}</span>
            </div>
          </article>
        );
      })}
    </div>
  );
}

function CandidateReviewList({ items }: { items: unknown[] }) {
  if (!items.length) return <div className="empty-state-small">暂无候选审查</div>;
  return (
    <div className="brain-action-plan-list">
      {items.slice(0, 8).map((raw, index) => {
        const item = asRecord(raw);
        const status = pickString(item, ["review_status"], "unknown");
        const conflict = asRecord(pick(item, ["conflict"]));
        const bridgePreview = asRecord(pick(item, ["bridge_preview"]));
        const gaps = pickArray(item, ["evidence_gaps"]);
        const bridgeReady = pickBoolean(item, ["bridge_ready"], false);
        const tone: Tone = bridgeReady ? "ok" : status === "conflict_detected" ? "bad" : gaps.length ? "warn" : "mute";
        return (
          <article className="brain-action-plan brain-action-plan-compact" key={`${pickString(item, ["review_id"], "review")}-${index}`}>
            <div className="brain-hypothesis-head">
              <div>
                <strong>{displayValue(status)}</strong>
                <span>{pickString(item, ["candidate.scope_type"], "scope")} · {displayAction(pickString(item, ["candidate.action"], "action"))}</span>
              </div>
              <StatusPill status={displayValue(status)} tone={tone} />
            </div>
            <CompactFacts facts={[
              { label: "桥接", value: bridgeReady ? "ready" : "blocked", tone: bridgeReady ? "ok" : "warn" },
              { label: "缺口", value: `${gaps.length}`, tone: gaps.length ? "warn" : "ok" },
              { label: "冲突", value: pickBoolean(conflict, ["has_conflict"], false) ? "yes" : "no", tone: pickBoolean(conflict, ["has_conflict"], false) ? "bad" : "ok" },
              { label: "LLM", value: pickBoolean(item, ["llm_advisory.enabled"], false) ? pickString(item, ["llm_advisory.status"], "enabled") : "off", tone: "mute" },
            ]} />
            <div className="brain-ref-row">
              <span>{pickString(conflict, ["surface"], "--")}</span>
              <span>{pickString(bridgePreview, ["reason"], pickString(item, ["bridge_reason"], "--"))}</span>
              <span>{gaps.slice(0, 2).map(String).join(", ") || "evidence ok"}</span>
            </div>
          </article>
        );
      })}
    </div>
  );
}

function GuardrailList({ items }: { items: unknown[] }) {
  if (!items.length) return <div className="empty-state-small">暂无 live-ready guardrail</div>;
  return (
    <div className="brain-action-plan-list">
      {items.slice(0, 8).map((raw, index) => {
        const item = asRecord(raw);
        const status = pickString(item, ["status"], "unknown");
        const lock = asRecord(pick(item, ["live_capability_lock"]));
        const divergence = asRecord(pick(item, ["broker_local_divergence"]));
        const incident = asRecord(pick(item, ["incident_control"]));
        const rollback = asRecord(pick(item, ["release_rollback"]));
        const recommendation = asRecord(pick(item, ["action_recommendation"]));
        const locked = pickBoolean(lock, ["locked"], false);
        const divergent = pickBoolean(divergence, ["divergence_detected"], false);
        return (
          <article className="brain-action-plan brain-action-plan-compact" key={`${pickString(item, ["guardrail_id"], "guardrail")}-${index}`}>
            <div className="brain-hypothesis-head">
              <div>
                <strong>{displayAction(pickString(recommendation, ["action"], "guardrail"))}</strong>
                <span>{formatTime(pick(item, ["created_at"]))} · {displayValue(pickString(recommendation, ["target_mode"], "--"))}</span>
              </div>
              <StatusPill status={displayValue(status)} tone={locked ? "ok" : status.includes("attention") ? "warn" : "mute"} />
            </div>
            <CompactFacts facts={[
              { label: "能力", value: displayValue(locked ? "locked" : "blocked"), tone: locked ? "ok" : "warn" },
              { label: "偏差", value: displayValue(pickString(divergence, ["status"], "unknown")), tone: divergent ? "bad" : "ok" },
              { label: "事故", value: displayValue(pickString(incident, ["mode"], "normal")), tone: pickString(incident, ["mode"], "normal") === "normal" ? "ok" : "warn" },
              { label: "回滚", value: displayValue(pickBoolean(rollback, ["rollback_ready"], false) ? "ready" : "missing"), tone: boolTone(pickBoolean(rollback, ["rollback_ready"], false)) },
            ]} />
            <div className="brain-ref-row">
              <span>broker {pickString(divergence, ["broker_open_count"], "--")}</span>
              <span>local {pickString(divergence, ["local_open_count"], "--")}</span>
              <span>{pickArray(recommendation, ["reasons"]).slice(0, 2).map(String).join(", ") || "no blocker"}</span>
            </div>
          </article>
        );
      })}
    </div>
  );
}

function ProposalRegistryList({
  items,
  onReview,
  reviewing,
}: {
  items: unknown[];
  onReview: (proposalId: string, route: string) => void;
  reviewing: boolean;
}) {
  if (!items.length) return <div className="empty-state-small">暂无提案</div>;
  return (
    <div className="brain-action-plan-list">
      {items.slice(0, 10).map((raw, index) => {
        const item = asRecord(raw);
        const proposalId = pickString(item, ["proposal_id"], "");
        const status = pickString(item, ["status"], "unknown");
        const conflict = asRecord(pick(item, ["conflict"]));
        const hasConflict = pickBoolean(conflict, ["conflict"], false);
        const route = pickString(item, ["route_recommendation"], "observe");
        const reliability = asRecord(pick(item, ["source_reliability"]));
        const freshness = asRecord(pick(item, ["evidence_freshness"]));
        const reliabilityBand = pickString(reliability, ["band"], "unknown");
        const freshnessStatus = pickString(freshness, ["status"], "unknown");
        return (
          <article className="brain-action-plan brain-action-plan-compact" key={`${proposalId || "proposal"}-${index}`}>
            <div className="brain-hypothesis-head">
              <div>
                <strong>{displayValue(pickString(item, ["control_surface"], "proposal_registry"))}</strong>
                <span title={pickString(item, ["target_scope"], "--")}>{pickString(item, ["target_scope"], "--")}</span>
              </div>
              <StatusPill status={displayValue(status)} tone={status.includes("blocked") || hasConflict ? "bad" : status === "reviewed" ? "ok" : "warn"} />
            </div>
            <CompactFacts facts={[
              { label: "来源", value: displayValue(pickString(item, ["source_agent"], "unknown")), tone: "mute" },
              { label: "影响", value: displayValue(pickString(item, ["impact_level"], "observe")), tone: riskTone(pickString(item, ["impact_level"], "observe")) },
              { label: "可信", value: `${displayValue(reliabilityBand)} ${formatDecimal(scorePct(pickNumber(reliability, ["score"], 0)), 0)}%`, tone: reliabilityBand === "low" ? "warn" : reliabilityBand === "high" ? "ok" : "mute" },
              { label: "新鲜", value: displayValue(freshnessStatus), tone: freshnessStatus === "fresh" ? "ok" : "warn" },
              { label: "路由", value: displayValue(route), tone: route === "request_review" ? "warn" : "mute" },
            ]} />
            <div className="brain-ref-row brain-ref-row-actions">
              <span title={proposalId}>{proposalId || "--"}</span>
              <span>{hasConflict ? displayValue(pickString(conflict, ["severity"], "conflict_detected")) : "无冲突"}</span>
              <button
                className="brain-inline-button"
                type="button"
                disabled={!proposalId || reviewing}
                onClick={() => onReview(proposalId, route)}
              >
                {reviewing ? "记录中" : "记录审查"}
              </button>
            </div>
          </article>
        );
      })}
    </div>
  );
}

export function V16BrainPage() {
  const queryClient = useQueryClient();
  const readinessQuery = useQuery({ queryKey: ["v16", "readiness"], queryFn: getBackendReadiness, refetchInterval: 15_000, staleTime: 5_000 });
  const brainStateQuery = useQuery({ queryKey: ["v16", "brain-state"], queryFn: () => getBrainState(false), refetchInterval: 20_000, staleTime: 8_000 });
  const brainMemoryQuery = useQuery({ queryKey: ["v16", "brain-memory"], queryFn: () => getBrainMemory(false, 80), refetchInterval: 30_000, staleTime: 10_000 });
  const brainActionPlansQuery = useQuery({ queryKey: ["v16", "brain-action-plans"], queryFn: () => getBrainActionPlans(false, 80), refetchInterval: 30_000, staleTime: 10_000 });
  const brainActionPlanEvalsQuery = useQuery({ queryKey: ["v16", "brain-action-plan-evals"], queryFn: () => getBrainActionPlanEvals(false, 80), refetchInterval: 30_000, staleTime: 10_000 });
  const lowImpactExecutionsQuery = useQuery({ queryKey: ["v16", "brain-low-impact-executions"], queryFn: () => getBrainLowImpactExecutions(80), refetchInterval: 30_000, staleTime: 10_000 });
  const mediumImpactGovernanceQuery = useQuery({ queryKey: ["v16", "brain-medium-impact-governance"], queryFn: () => getBrainMediumImpactGovernance(80), refetchInterval: 30_000, staleTime: 10_000 });
  const candidateReviewsQuery = useQuery({ queryKey: ["v16", "brain-governance-candidate-reviews"], queryFn: () => getBrainGovernanceCandidateReviews(80), refetchInterval: 30_000, staleTime: 10_000 });
  const liveReadyGuardrailsQuery = useQuery({ queryKey: ["v16", "brain-live-ready-guardrails"], queryFn: () => getBrainLiveReadyGuardrails(80), refetchInterval: 30_000, staleTime: 10_000 });
  const proposalRegistryQuery = useQuery({ queryKey: ["autonomy", "proposal-registry"], queryFn: () => getAutonomyProposals(false, 80), refetchInterval: 30_000, staleTime: 10_000 });
  const liveAutonomyQuery = useQuery({ queryKey: ["autonomy", "live-status"], queryFn: () => getLiveAutonomyStatus(false), refetchInterval: 20_000, staleTime: 8_000 });

  const refreshMutation = useMutation({
    mutationFn: async () => {
      await getBrainState(true);
      await getBrainMemory(true, 80);
      await getBrainActionPlans(true, 80);
      await getBrainActionPlanEvals(true, 80);
      await getBrainLowImpactExecutions(80);
      await getBrainMediumImpactGovernance(80);
      await getBrainGovernanceCandidateReviews(80);
      await getBrainLiveReadyGuardrails(80);
      await getAutonomyProposals(true, 80);
      await getLiveAutonomyStatus(true);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["v16"] });
      void queryClient.invalidateQueries({ queryKey: ["autonomy"] });
    },
  });

  const lowImpactMutation = useMutation({
    mutationFn: runBrainLowImpactExecution,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["v16"] });
    },
  });

  const mediumImpactMutation = useMutation({
    mutationFn: materializeBrainMediumImpactGovernance,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["v16"] });
    },
  });

  const candidateReviewMutation = useMutation({
    mutationFn: reviewBrainGovernanceCandidates,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["v16"] });
    },
  });

  const liveReadyEvaluateMutation = useMutation({
    mutationFn: evaluateBrainLiveReadyGuardrail,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["v16"] });
    },
  });

  const liveReadyTightenMutation = useMutation({
    mutationFn: tightenBrainLiveReadyGuardrail,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["v16"] });
      void queryClient.invalidateQueries({ queryKey: ["autonomy"] });
    },
  });

  const proposalRefreshMutation = useMutation({
    mutationFn: () => refreshAutonomyProposals(500),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["autonomy"] });
      void queryClient.invalidateQueries({ queryKey: ["v16"] });
    },
  });

  const proposalReviewMutation = useMutation({
    mutationFn: ({ proposalId, route }: { proposalId: string; route: string }) =>
      reviewAutonomyProposal(proposalId, {
        decision: "reviewed",
        route,
        notes: "web meta governance review",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["autonomy"] });
      void queryClient.invalidateQueries({ queryKey: ["v16"] });
    },
  });

  const liveUnlockEvaluateMutation = useMutation({
    mutationFn: () => evaluateLiveAutonomyUnlock(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["autonomy"] });
      void queryClient.invalidateQueries({ queryKey: ["v16"] });
    },
  });

  const liveUnlockMutation = useMutation({
    mutationFn: async () => {
      if (!window.confirm("确认一次性解锁实盘自治？")) {
        return { ok: false, status: "cancelled" };
      }
      return unlockLiveAutonomy();
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["autonomy"] });
      void queryClient.invalidateQueries({ queryKey: ["v16"] });
    },
  });

  const liveRevokeMutation = useMutation({
    mutationFn: () => revokeLiveAutonomy(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["autonomy"] });
      void queryClient.invalidateQueries({ queryKey: ["v16"] });
    },
  });

  const readiness = asRecord(readinessQuery.data);
  const v16Readiness = asRecord(pick(readiness, ["v16"]));
  const directBrainState = asRecord(pick(brainStateQuery.data, ["brain_state"]));
  const readinessBrainState = asRecord(pick(v16Readiness, ["brain_state.latest_snapshot"]));
  const brainState = Object.keys(directBrainState).length ? directBrainState : readinessBrainState;
  const worldModel = asRecord(pick(brainState, ["world_model"]));
  const memoryFromState = asRecord(pick(brainState, ["memory"]));
  const memoryFromQuery = asRecord(pick(brainMemoryQuery.data, ["memory"]));
  const memory = Object.keys(memoryFromState).length ? memoryFromState : memoryFromQuery;
  const critic = asRecord(pick(brainState, ["critic"]));
  const hypotheses = pickArray(brainState, ["hypotheses"]);
  const memoryItems = pickArray(memory, ["items"]);
  const negativeMemory = pickArray(memory, ["negative_matches"]);
  const counterEvidence = pickArray(memory, ["counter_evidence"]);
  const evidenceRefs = asRecord(pick(brainState, ["evidence_refs"]));
  const boundary = asRecord(pick(brainState, ["boundary"]));
  const sourceGaps = pickArray(memory, ["source_gaps"]);
  const directActionPlanRun = asRecord(pick(brainActionPlansQuery.data, ["action_plans"]));
  const readinessActionPlans = asRecord(pick(v16Readiness, ["action_plans"]));
  const actionPlanRun = Object.keys(directActionPlanRun).length ? directActionPlanRun : readinessActionPlans;
  const actionPlans = pickArray(actionPlanRun, ["plans"]);
  const directActionPlanEvals = asRecord(pick(brainActionPlanEvalsQuery.data, ["action_plan_evals"]));
  const readinessActionPlanEvals = asRecord(pick(v16Readiness, ["action_plan_evals"]));
  const actionPlanEvalRun = Object.keys(directActionPlanEvals).length ? directActionPlanEvals : readinessActionPlanEvals;
  const actionPlanEvals = pickArray(actionPlanEvalRun, ["evals"]);
  const directLowImpactExecutions = asRecord(pick(lowImpactExecutionsQuery.data, ["low_impact_executions"]));
  const readinessLowImpactExecutions = asRecord(pick(v16Readiness, ["low_impact_executions"]));
  const lowImpactExecutionRun = Object.keys(directLowImpactExecutions).length ? directLowImpactExecutions : readinessLowImpactExecutions;
  const lowImpactExecutions = pickArray(lowImpactExecutionRun, ["executions"]);
  const directMediumImpactGovernance = asRecord(pick(mediumImpactGovernanceQuery.data, ["medium_impact_governance"]));
  const readinessMediumImpactGovernance = asRecord(pick(v16Readiness, ["medium_impact_governance"]));
  const mediumImpactGovernanceRun = Object.keys(directMediumImpactGovernance).length ? directMediumImpactGovernance : readinessMediumImpactGovernance;
  const mediumImpactGovernance = pickArray(mediumImpactGovernanceRun, ["items"]);
  const directCandidateReviews = asRecord(pick(candidateReviewsQuery.data, ["candidate_reviews"]));
  const readinessCandidateReviews = asRecord(pick(v16Readiness, ["governance_candidate_reviews"]));
  const candidateReviewRun = Object.keys(directCandidateReviews).length ? directCandidateReviews : readinessCandidateReviews;
  const candidateReviews = pickArray(candidateReviewRun, ["items"]);
  const directLiveReadyGuardrails = asRecord(pick(liveReadyGuardrailsQuery.data, ["live_ready_guardrails"]));
  const readinessLiveReadyGuardrails = asRecord(pick(v16Readiness, ["live_ready_guardrails"]));
  const liveReadyGuardrailRun = Object.keys(directLiveReadyGuardrails).length ? directLiveReadyGuardrails : readinessLiveReadyGuardrails;
  const liveReadyGuardrails = pickArray(liveReadyGuardrailRun, ["items"]);
  const latestGuardrail = asRecord(liveReadyGuardrails[0]);
  const directProposalRegistry = asRecord(pick(proposalRegistryQuery.data, ["proposals"]));
  const readinessProposalRegistry = asRecord(pick(v16Readiness, ["proposal_registry"]));
  const proposalRegistry = Object.keys(directProposalRegistry).length ? directProposalRegistry : readinessProposalRegistry;
  const proposalItems = pickArray(proposalRegistry, ["items"]);
  const proposalSummary = asRecord(pick(proposalRegistry, ["summary"]));
  const directLiveAutonomy = asRecord(pick(liveAutonomyQuery.data, ["live_autonomy"]));
  const readinessLiveAutonomy = asRecord(pick(v16Readiness, ["live_autonomy"]));
  const liveAutonomy = Object.keys(directLiveAutonomy).length ? directLiveAutonomy : readinessLiveAutonomy;
  const liveAutonomyEvaluation = asRecord(pick(liveAutonomy, ["evaluation"]));
  const liveAutonomyBlockers = pickArray(liveAutonomyEvaluation, ["blockers"]);
  const liveAutonomyLatestEvent = asRecord(pick(liveAutonomy, ["latest_event"]));
  const liveAutonomyPosture = asRecord(pick(liveAutonomy, ["operational_posture"]));

  const statTone = useMemo<Tone>(() => {
    const posture = pickString(worldModel, ["strategy_posture"], "");
    if (["normal"].includes(posture)) return "ok";
    if (["no_new_risk", "observation_only"].includes(posture)) return "bad";
    return "warn";
  }, [worldModel]);
  const criticVerdict = pickString(critic, ["verdict"], pickString(brainState, ["critic_verdict"], "unknown"));
  const readOnly = pickBoolean(brainState, ["read_only"], true) && pickBoolean(boundary, ["read_only"], true);
  const affectsTrading = pickBoolean(brainState, ["affects_trading"], false);

  return (
    <section className="dashboard v16-dashboard">
      <div className="dashboard-header">
        <div>
          <div className="eyebrow">Autonomy Brain</div>
          <h1>自治大脑</h1>
          <p>查看世界模型、影子计划、后验评价、治理候选和实盘护栏。</p>
        </div>
        <div className="header-status">
          <StatusPill status={displayStage(pickString(v16Readiness, ["phase"], "phase5_live_ready_guardrails"))} tone="ok" />
          <StatusPill status={readOnly && !affectsTrading ? "交易边界正常" : "边界异常"} tone={readOnly && !affectsTrading ? "ok" : "bad"} />
          <button className="header-refresh" type="button" disabled={refreshMutation.isPending} onClick={() => refreshMutation.mutate()}>
            <RefreshCw size={15} aria-hidden="true" />
            {refreshMutation.isPending ? "刷新中" : "刷新大脑"}
          </button>
          <button className="header-refresh" type="button" disabled={lowImpactMutation.isPending} onClick={() => lowImpactMutation.mutate()}>
            <Play size={15} aria-hidden="true" />
            {lowImpactMutation.isPending ? "运行中" : "低影响回放"}
          </button>
          <button className="header-refresh" type="button" disabled={mediumImpactMutation.isPending} onClick={() => mediumImpactMutation.mutate()}>
            <ListChecks size={15} aria-hidden="true" />
            {mediumImpactMutation.isPending ? "生成中" : "生成治理候选"}
          </button>
          <button className="header-refresh" type="button" disabled={candidateReviewMutation.isPending} onClick={() => candidateReviewMutation.mutate()}>
            <GitBranch size={15} aria-hidden="true" />
            {candidateReviewMutation.isPending ? "审查中" : "审查候选"}
          </button>
          <button className="header-refresh" type="button" disabled={liveReadyEvaluateMutation.isPending} onClick={() => liveReadyEvaluateMutation.mutate()}>
            <ShieldCheck size={15} aria-hidden="true" />
            {liveReadyEvaluateMutation.isPending ? "评估中" : "护栏评估"}
          </button>
          <button className="header-refresh" type="button" disabled={liveReadyTightenMutation.isPending} onClick={() => liveReadyTightenMutation.mutate("no_new_risk")}>
            <ShieldCheck size={15} aria-hidden="true" />
            不增风险
          </button>
          <button className="header-refresh" type="button" disabled={liveReadyTightenMutation.isPending} onClick={() => liveReadyTightenMutation.mutate("only_close")}>
            <ShieldCheck size={15} aria-hidden="true" />
            仅平仓
          </button>
          <button className="header-refresh" type="button" disabled={liveReadyTightenMutation.isPending} onClick={() => liveReadyTightenMutation.mutate("frozen")}>
            <ShieldCheck size={15} aria-hidden="true" />
            冻结
          </button>
          {refreshMutation.isError ? <span className="error-text small">刷新失败</span> : null}
          {lowImpactMutation.isError ? <span className="error-text small">低影响回放失败</span> : null}
          {mediumImpactMutation.isError ? <span className="error-text small">治理候选失败</span> : null}
          {candidateReviewMutation.isError ? <span className="error-text small">候选审查失败</span> : null}
          {liveReadyEvaluateMutation.isError ? <span className="error-text small">护栏评估失败</span> : null}
          {liveReadyTightenMutation.isError ? <span className="error-text small">收紧失败</span> : null}
        </div>
      </div>

      <div className="stat-grid v15-stat-grid">
        <StatTile icon={BrainCircuit} label="策略姿态" value={displayValue(pickString(worldModel, ["strategy_posture"], "unknown"))} detail={displayValue(pickString(worldModel, ["market_regime"], "unknown"))} tone={statTone} />
        <StatTile icon={ShieldCheck} label="Critic" value={displayValue(criticVerdict)} detail={displayValue(pickString(critic, ["max_allowed_action_scope"], "observe_only"))} tone={criticVerdict === "pass" ? "ok" : "warn"} />
        <StatTile icon={Database} label="记忆命中" value={formatDecimal(memoryItems.length, 0)} detail={`负面 ${formatDecimal(negativeMemory.length, 0)} / 反证 ${formatDecimal(counterEvidence.length, 0)}`} tone={negativeMemory.length ? "warn" : "ok"} />
        <StatTile icon={Workflow} label="假设" value={formatDecimal(hypotheses.length, 0)} detail={formatTime(pick(brainState, ["created_at"]))} tone={hypotheses.length ? "ok" : "warn"} />
        <StatTile icon={ListChecks} label="影子计划" value={formatDecimal(actionPlans.length, 0)} detail={displayValue(pickString(actionPlanRun, ["status"], "shadow"))} tone={actionPlans.length ? "ok" : "warn"} />
        <StatTile icon={GitBranch} label="后验评价" value={formatDecimal(actionPlanEvals.length, 0)} detail={displayValue(pickString(actionPlanEvalRun, ["status"], "posterior"))} tone={actionPlanEvals.length ? "ok" : "warn"} />
        <StatTile icon={Play} label="低影响执行" value={formatDecimal(lowImpactExecutions.length, 0)} detail={displayValue(pickString(lowImpactExecutionRun, ["status"], "low-impact"))} tone={lowImpactExecutions.length ? "ok" : "warn"} />
        <StatTile icon={ListChecks} label="治理候选" value={formatDecimal(mediumImpactGovernance.length, 0)} detail={displayValue(pickString(mediumImpactGovernanceRun, ["status"], "medium-impact"))} tone={mediumImpactGovernance.length ? "ok" : "warn"} />
        <StatTile icon={GitBranch} label="候选审查" value={formatDecimal(candidateReviews.length, 0)} detail={displayValue(pickString(candidateReviewRun, ["status"], "review"))} tone={candidateReviews.length ? "ok" : "warn"} />
        <StatTile icon={ListChecks} label="提案总线" value={formatDecimal(pickNumber(proposalSummary, ["proposal_count"], proposalItems.length), 0)} detail={`冲突 ${formatDecimal(pickNumber(proposalSummary, ["conflict_count"], 0), 0)}`} tone={pickNumber(proposalSummary, ["high_unresolved_conflict_count"], 0) ? "bad" : "ok"} />
        <StatTile icon={ShieldCheck} label="实盘自治" value={displayValue(pickString(liveAutonomy, ["autonomy_mode"], "manual"))} detail={displayValue(pickString(liveAutonomyEvaluation, ["status"], "locked"))} tone={pickBoolean(liveAutonomy, ["live_autonomy_unlocked"], false) ? "ok" : liveAutonomyBlockers.length ? "warn" : "mute"} />
        <StatTile icon={ShieldCheck} label="实盘护栏" value={pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "已锁定" : formatDecimal(liveReadyGuardrails.length, 0)} detail={displayValue(pickString(liveReadyGuardrailRun, ["status"], pickString(latestGuardrail, ["action_recommendation.target_mode"], "live-ready")))} tone={pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "ok" : "warn"} />
      </div>

      <div className="dashboard-grid v16-grid">
        <MetricCard title="世界模型" className="wide-panel">
          <div className="v15-mini-grid">
            <CompactMetric label="市场状态" value={displayValue(pickString(worldModel, ["market_regime"], "unknown"))} tone="mute" />
            <CompactMetric label="因子姿态" value={displayValue(pickString(worldModel, ["factor_posture"], "unknown"))} tone={toneFromStatus(pickString(worldModel, ["factor_posture"], "unknown"))} />
            <CompactMetric label="执行姿态" value={displayValue(pickString(worldModel, ["execution_posture"], "unknown"))} tone={toneFromStatus(pickString(worldModel, ["execution_posture"], "unknown"))} />
            <CompactMetric label="学习姿态" value={displayValue(pickString(worldModel, ["learning_posture"], "unknown"))} tone="mute" />
            <CompactMetric label="自治姿态" value={displayValue(pickString(worldModel, ["autonomy_posture"], "unknown"))} tone={toneFromStatus(pickString(worldModel, ["autonomy_posture"], "unknown"))} />
            <CompactMetric label="事故模式" value={displayValue(pickString(worldModel, ["incident_mode"], "normal"))} tone={pickString(worldModel, ["incident_mode"], "normal") === "normal" ? "ok" : "warn"} />
          </div>
          <div className="v16-boundary">
            <Field label="只读" value={readOnly ? "true" : "false"} tone={boolTone(readOnly)} />
            <Field label="影响交易" value={affectsTrading ? "true" : "false"} tone={affectsTrading ? "bad" : "ok"} />
            <Field label="不执行计划" value={pickBoolean(boundary, ["does_not_execute_action_plan"], false) ? "true" : "false"} tone={boolTone(pickBoolean(boundary, ["does_not_execute_action_plan"], false))} />
            <Field label="不写学习样本" value={pickBoolean(boundary, ["does_not_write_learning_samples"], false) ? "true" : "false"} tone={boolTone(pickBoolean(boundary, ["does_not_write_learning_samples"], false))} />
          </div>
        </MetricCard>

        <MetricCard title="提案总线" className="wide-panel">
          <div className="v16-boundary">
            <Field label="提案" value={formatDecimal(pickNumber(proposalSummary, ["proposal_count"], proposalItems.length), 0)} />
            <Field label="活跃" value={formatDecimal(pickNumber(proposalSummary, ["active_count"], 0), 0)} />
            <Field label="冲突" value={formatDecimal(pickNumber(proposalSummary, ["conflict_count"], 0), 0)} tone={pickNumber(proposalSummary, ["conflict_count"], 0) ? "warn" : "ok"} />
            <Field label="高危未解" value={formatDecimal(pickNumber(proposalSummary, ["high_unresolved_conflict_count"], 0), 0)} tone={pickNumber(proposalSummary, ["high_unresolved_conflict_count"], 0) ? "bad" : "ok"} />
            <Field label="低可信" value={formatDecimal(pickNumber(proposalSummary, ["low_reliability_count"], 0), 0)} tone={pickNumber(proposalSummary, ["low_reliability_count"], 0) ? "warn" : "ok"} />
            <Field label="证据过期" value={formatDecimal(pickNumber(proposalSummary, ["stale_evidence_count"], 0), 0)} tone={pickNumber(proposalSummary, ["stale_evidence_count"], 0) ? "warn" : "ok"} />
            <Field label="契约" value={displayContract(pickString(proposalRegistry, ["schema_version"], "--"))} />
          </div>
          <div className="brain-card-actions">
            <button className="header-refresh" type="button" disabled={proposalRefreshMutation.isPending} onClick={() => proposalRefreshMutation.mutate()}>
              <RefreshCw size={15} aria-hidden="true" />
              {proposalRefreshMutation.isPending ? "刷新中" : "刷新总线"}
            </button>
            {proposalRefreshMutation.isError ? <span className="error-text small">总线刷新失败</span> : null}
            {proposalReviewMutation.isError ? <span className="error-text small">审查记录失败</span> : null}
          </div>
          <ProposalRegistryList
            items={proposalItems}
            reviewing={proposalReviewMutation.isPending}
            onReview={(proposalId, route) => proposalReviewMutation.mutate({ proposalId, route })}
          />
        </MetricCard>

        <MetricCard title="实盘自治" className="wide-panel">
          <div className="v16-boundary">
            <Field label="模式" value={displayValue(pickString(liveAutonomy, ["autonomy_mode"], "manual"))} tone={pickBoolean(liveAutonomy, ["live_autonomy_unlocked"], false) ? "ok" : "warn"} />
            <Field label="解锁" value={pickBoolean(liveAutonomy, ["live_autonomy_unlocked"], false) ? "true" : "false"} tone={boolTone(pickBoolean(liveAutonomy, ["live_autonomy_unlocked"], false))} />
            <Field label="姿态" value={displayValue(pickString(liveAutonomyPosture, ["status"], "locked"))} tone={pickString(liveAutonomyPosture, ["status"], "locked") === "degraded" ? "bad" : "ok"} />
            <Field label="评估" value={displayValue(pickString(liveAutonomyEvaluation, ["status"], "blocked"))} tone={pickBoolean(liveAutonomyEvaluation, ["ok"], false) ? "ok" : "warn"} />
            <Field label="阻断项" value={formatDecimal(liveAutonomyBlockers.length, 0)} tone={liveAutonomyBlockers.length ? "warn" : "ok"} />
            <Field label="建议模式" value={displayValue(pickString(liveAutonomyPosture, ["recommended_incident_mode"], "normal"))} tone={pickString(liveAutonomyPosture, ["recommended_incident_mode"], "normal") === "normal" ? "ok" : "warn"} />
            <Field label="最近事件" value={displayValue(pickString(liveAutonomyLatestEvent, ["status"], "none"))} />
          </div>
          <div className="brain-card-actions">
            <button className="header-refresh" type="button" disabled={liveUnlockEvaluateMutation.isPending} onClick={() => liveUnlockEvaluateMutation.mutate()}>
              <ShieldCheck size={15} aria-hidden="true" />
              {liveUnlockEvaluateMutation.isPending ? "评估中" : "评估解锁"}
            </button>
            <button className="header-refresh" type="button" disabled={liveUnlockMutation.isPending || !pickBoolean(liveAutonomyEvaluation, ["ok"], false)} onClick={() => liveUnlockMutation.mutate()}>
              <ShieldCheck size={15} aria-hidden="true" />
              {liveUnlockMutation.isPending ? "解锁中" : "一次解锁"}
            </button>
            <button className="header-refresh" type="button" disabled={liveRevokeMutation.isPending || !pickBoolean(liveAutonomy, ["live_autonomy_unlocked"], false)} onClick={() => liveRevokeMutation.mutate()}>
              <ShieldCheck size={15} aria-hidden="true" />
              {liveRevokeMutation.isPending ? "撤销中" : "撤销自治"}
            </button>
            {liveUnlockEvaluateMutation.isError ? <span className="error-text small">解锁评估失败</span> : null}
            {liveUnlockMutation.isError ? <span className="error-text small">解锁失败</span> : null}
            {liveRevokeMutation.isError ? <span className="error-text small">撤销失败</span> : null}
          </div>
          <div className="v15-list compact-v15-list">
            {(liveAutonomyBlockers.length ? liveAutonomyBlockers : [{ component: "unlock", status: "ok", reason: "ready" }]).slice(0, 6).map((raw, index) => {
              const item = asRecord(raw);
              const component = pickString(item, ["component"], "unlock");
              const status = pickString(item, ["status"], pickString(item, ["reason"], "ok"));
              return (
                <div className="v15-list-row" key={`${component}-${status}-${index}`}>
                  <div>
                    <strong>{displayValue(component)}</strong>
                    <span>{displayValue(pickString(item, ["reason"], status))}</span>
                  </div>
                  <StatusPill status={displayValue(status)} tone={status === "ok" || status === "ready" ? "ok" : "warn"} />
                </div>
              );
            })}
          </div>
        </MetricCard>

        <MetricCard title="假设">
          <HypothesisList items={hypotheses} />
        </MetricCard>

        <MetricCard title="记忆">
          <SectionHead title="负面记忆" status={`${negativeMemory.length}`} tone={negativeMemory.length ? "warn" : "ok"} />
          <MemoryList items={negativeMemory} empty="暂无负面记忆命中" />
          <SectionHead title="反证" status={`${counterEvidence.length}`} tone={counterEvidence.length ? "ok" : "mute"} />
          <MemoryList items={counterEvidence} empty="暂无反证命中" />
        </MetricCard>

        <MetricCard title="影子计划" className="wide-panel">
          <div className="v16-boundary">
            <Field label="只读计划" value={pickBoolean(actionPlanRun, ["read_only"], true) ? "true" : "false"} tone={boolTone(pickBoolean(actionPlanRun, ["read_only"], true))} />
            <Field label="影响交易" value={pickBoolean(actionPlanRun, ["affects_trading"], false) ? "true" : "false"} tone={pickBoolean(actionPlanRun, ["affects_trading"], false) ? "bad" : "ok"} />
            <Field label="阶段" value={displayStage(pickString(actionPlanRun, ["phase"], "v16_phase2_shadow_brain"))} />
            <Field label="快照" value={pickString(actionPlanRun, ["snapshot_id"], pickString(brainState, ["snapshot_id"], "--"))} />
          </div>
          <ActionPlanList items={actionPlans} />
        </MetricCard>

        <MetricCard title="后验评价" className="wide-panel">
          <div className="v16-boundary">
            <Field label="只读评价" value={pickBoolean(actionPlanEvalRun, ["read_only"], true) ? "true" : "false"} tone={boolTone(pickBoolean(actionPlanEvalRun, ["read_only"], true))} />
            <Field label="影响交易" value={pickBoolean(actionPlanEvalRun, ["affects_trading"], false) ? "true" : "false"} tone={pickBoolean(actionPlanEvalRun, ["affects_trading"], false) ? "bad" : "ok"} />
            <Field label="评价契约" value={displayContract(pickString(actionPlanEvalRun, ["schema_version"], "--"))} />
            <Field label="证据缺口" value={formatDecimal(pickArray(actionPlanEvalRun, ["source_gaps"]).length, 0)} tone={pickArray(actionPlanEvalRun, ["source_gaps"]).length ? "warn" : "ok"} />
          </div>
          <EvaluationList items={actionPlanEvals} />
        </MetricCard>

        <MetricCard title="低影响执行" className="wide-panel">
          <div className="v16-boundary">
            <Field label="白名单" value={pickBoolean(lowImpactExecutionRun, ["boundary.low_impact_only"], true) ? "true" : "false"} tone={boolTone(pickBoolean(lowImpactExecutionRun, ["boundary.low_impact_only"], true))} />
            <Field label="RiskPolicy" value={pickBoolean(lowImpactExecutionRun, ["boundary.risk_policy_service_required"], true) ? "required" : "missing"} tone={boolTone(pickBoolean(lowImpactExecutionRun, ["boundary.risk_policy_service_required"], true))} />
            <Field label="执行契约" value={displayContract(pickString(lowImpactExecutionRun, ["schema_version"], "--"))} />
            <Field label="最近更新" value={formatTime(pick(lowImpactExecutionRun, ["latest_created_at"]))} />
          </div>
          <ExecutionList items={lowImpactExecutions} />
        </MetricCard>

        <MetricCard title="治理候选" className="wide-panel">
          <div className="v16-boundary">
            <Field label="只生成候选" value={pickBoolean(mediumImpactGovernanceRun, ["boundary.materializes_governance_candidates_only"], true) ? "true" : "false"} tone={boolTone(pickBoolean(mediumImpactGovernanceRun, ["boundary.materializes_governance_candidates_only"], true))} />
            <Field label="手动桥接" value={pickBoolean(mediumImpactGovernanceRun, ["boundary.policy_suggestion_bridge_manual_only"], true) ? "true" : "false"} tone={boolTone(pickBoolean(mediumImpactGovernanceRun, ["boundary.policy_suggestion_bridge_manual_only"], true))} />
            <Field label="不改权重" value={pickBoolean(mediumImpactGovernanceRun, ["boundary.does_not_apply_factor_weights"], true) ? "true" : "false"} tone={boolTone(pickBoolean(mediumImpactGovernanceRun, ["boundary.does_not_apply_factor_weights"], true))} />
            <Field label="治理契约" value={displayContract(pickString(mediumImpactGovernanceRun, ["schema_version"], "--"))} />
            <Field label="最近更新" value={formatTime(pick(mediumImpactGovernanceRun, ["latest_created_at"]))} />
          </div>
          <GovernanceList items={mediumImpactGovernance} />
        </MetricCard>

        <MetricCard title="候选审查" className="wide-panel">
          <div className="v16-boundary">
            <Field label="只读审查" value={pickBoolean(candidateReviewRun, ["boundary.review_only"], true) ? "true" : "false"} tone={boolTone(pickBoolean(candidateReviewRun, ["boundary.review_only"], true))} />
            <Field label="桥接预览" value={pickBoolean(candidateReviewRun, ["boundary.bridge_preview_only"], true) ? "true" : "false"} tone={boolTone(pickBoolean(candidateReviewRun, ["boundary.bridge_preview_only"], true))} />
            <Field label="LLM 只建议" value={pickBoolean(candidateReviewRun, ["boundary.llm_advisory_only"], true) ? "true" : "false"} tone={boolTone(pickBoolean(candidateReviewRun, ["boundary.llm_advisory_only"], true))} />
            <Field label="最近更新" value={formatTime(pick(candidateReviewRun, ["latest_created_at"]))} />
          </div>
          <CandidateReviewList items={candidateReviews} />
        </MetricCard>

        <MetricCard title="实盘护栏" className="wide-panel">
          <div className="v16-boundary">
            <Field label="能力锁" value={displayValue(pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "locked" : "blocked")} tone={pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "ok" : "warn"} />
            <Field label="偏差" value={displayValue(pickString(latestGuardrail, ["broker_local_divergence.status"], pickString(liveReadyGuardrailRun, ["divergence_status"], "--")))} tone={pickString(latestGuardrail, ["broker_local_divergence.status"], "") === "divergent" ? "bad" : "ok"} />
            <Field label="事故" value={displayValue(pickString(latestGuardrail, ["incident_control.mode"], "--"))} tone={pickString(latestGuardrail, ["incident_control.mode"], "normal") === "normal" ? "ok" : "warn"} />
            <Field label="回滚" value={displayValue(pickBoolean(latestGuardrail, ["release_rollback.rollback_ready"], false) ? "ready" : "missing")} tone={boolTone(pickBoolean(latestGuardrail, ["release_rollback.rollback_ready"], false))} />
            <Field label="建议模式" value={displayValue(pickString(latestGuardrail, ["action_recommendation.target_mode"], pickString(liveReadyGuardrailRun, ["recommended_mode"], "--")))} />
            <Field label="最近更新" value={formatTime(pick(liveReadyGuardrailRun, ["latest_created_at"]))} />
          </div>
          <GuardrailList items={liveReadyGuardrails} />
        </MetricCard>

        <MetricCard title="Critic 与证据" className="wide-panel">
          <div className="v15-two-col">
            <div>
              <SectionHead title="Critic 异议" status={`${pickArray(critic, ["objections"]).length}`} tone={pickArray(critic, ["objections"]).length ? "warn" : "ok"} />
              <div className="v15-list">
                {(pickArray(critic, ["objections"]).length ? pickArray(critic, ["objections"]) : ["none"]).map((item, index) => (
                  <div className="v15-list-row" key={`${String(item)}-${index}`}>
                    <div>
                      <strong>{String(item)}</strong>
                      <span>{displayValue(pickString(critic, ["max_allowed_action_scope"], "observe_only"))}</span>
                    </div>
                    <StatusPill status={displayValue(criticVerdict)} tone={criticVerdict === "pass" ? "ok" : "warn"} />
                  </div>
                ))}
              </div>
            </div>
            <div>
              <SectionHead title="证据缺口" status={`${sourceGaps.length}`} tone={sourceGaps.length ? "warn" : "ok"} />
              <MemoryList items={sourceGaps.map((gap) => ({ source_table: "source_gap", source_id: String(gap), text_summary: String(gap), polarity: "neutral" }))} empty="无证据缺口" />
            </div>
          </div>
          <div className="brain-json-grid">
            <div>
              <SectionHead title="证据引用" />
              <JsonBlock value={evidenceRefs} />
            </div>
            <div>
              <SectionHead title="记忆查询" />
              <JsonBlock value={{ query_terms: pickArray(memory, ["query_terms"]), source_gaps: sourceGaps }} />
            </div>
          </div>
        </MetricCard>

        <MetricCard title="最近记忆索引" className="wide-panel">
          <MemoryList items={memoryItems} empty="暂无索引记忆" />
        </MetricCard>

        <MetricCard title="契约状态">
          <div className="field-list">
            <Field label="后端就绪" value={displayContract(pickString(readiness, ["schema_version"], "--"))} />
            <Field label="自治就绪" value={displayContract(pickString(v16Readiness, ["schema_version"], "--"))} />
            <Field label="大脑快照" value={pickString(brainState, ["snapshot_id"], "--")} />
            <Field label="记忆检索" value={displayContract(pickString(memory, ["schema_version"], "--"))} />
            <Field label="影子计划" value={displayContract(pickString(actionPlanRun, ["schema_version"], "--"))} />
            <Field label="后验评价" value={displayContract(pickString(actionPlanEvalRun, ["schema_version"], "--"))} />
            <Field label="低影响执行" value={displayContract(pickString(lowImpactExecutionRun, ["schema_version"], "--"))} />
            <Field label="治理候选" value={displayContract(pickString(mediumImpactGovernanceRun, ["schema_version"], "--"))} />
            <Field label="候选审查" value={displayContract(pickString(candidateReviewRun, ["schema_version"], "--"))} />
          </div>
          <div className="brain-ref-row">
            <GitBranch size={15} aria-hidden="true" />
            <span>{pickString(brainState, ["source"], "backend")}</span>
            <Sparkles size={15} aria-hidden="true" />
            <span>{displayStage(pickString(brainState, ["phase"], "v16_phase1_read_only_brain"))}</span>
          </div>
        </MetricCard>
      </div>
    </section>
  );
}
