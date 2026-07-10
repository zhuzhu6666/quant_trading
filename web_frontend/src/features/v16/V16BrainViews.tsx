import {
  Activity,
  AlertTriangle,
  BookOpenCheck,
  CheckCircle2,
  ListChecks,
  Network,
  Route,
  ShieldCheck,
  UsersRound,
} from "lucide-react";
import { CompactMetric, Field, type Tone } from "@/components/DashboardBits";
import { StatusPill } from "@/components/StatusPill";
import { formatDecimal } from "@/lib/format";

export function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

export function pick(value: unknown, path: string[]): unknown {
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

export function pickString(value: unknown, path: string[], fallback = "--"): string {
  const raw = pick(value, path);
  if (raw === null || raw === undefined || raw === "") return fallback;
  return String(raw);
}

export function pickNumber(value: unknown, path: string[], fallback = 0): number {
  const raw = pick(value, path);
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function pickBoolean(value: unknown, path: string[], fallback = false): boolean {
  const raw = pick(value, path);
  return typeof raw === "boolean" ? raw : fallback;
}

export function pickArray(value: unknown, path: string[]): unknown[] {
  const raw = pick(value, path);
  return Array.isArray(raw) ? raw : [];
}

export function formatTime(raw: unknown): string {
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) return "--";
  return new Date(value * 1000).toLocaleString();
}

export function scorePct(raw: number): number {
  if (!Number.isFinite(raw)) return 0;
  return raw <= 1 ? raw * 100 : raw;
}

export function boolTone(value: boolean): Tone {
  return value ? "ok" : "warn";
}

export function riskTone(value: string): Tone {
  const normalized = value.toLowerCase();
  if (["high", "critical", "bad"].includes(normalized)) return "bad";
  if (["medium", "warn"].includes(normalized)) return "warn";
  if (["low", "ok"].includes(normalized)) return "ok";
  return "mute";
}

export function displayStage(value: string): string {
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

export function displayAction(value: string): string {
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

export function displayValue(value: string): string {
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
    partial: "部分对齐",
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
    demo_autonomous: "Demo 自治",
    demo_nursery: "Demo 育苗",
    advisory_only: "只建议",
    review_only: "只审查",
    blocked_by_agent_authority: "权限阻断",
    v16_brain: "V16 大脑",
    autonomous_learning: "自主学习",
    factor_governance: "因子治理",
    factor_pruning_governance: "因子裁剪治理",
    llm_advisory: "LLM 建议",
    lightgbm_shadow_models: "LightGBM 影子",
    demo_nursery_learning_scope: "Demo 育苗范围",
    agent_authority_contract: "Agent 权责合同",
    proposal_generation_context: "提案上下文",
    candidate_generation_context: "候选上下文",
    candidate_bridge_review: "候选桥接审查",
    proposal_registry_read_model: "提案读模型",
    memory_and_scorecard_feedback: "记忆与评分反馈",
    single_execution_boundary: "单执行边界",
    live_ready_guardrails: "实盘护栏",
    unknown: "未知",
    warn: "注意",
  };
  return labels[normalized] || value.replace(/^v\d+_/, "").replace(/\.v\d+$/, "").replaceAll("_", " ");
}

export function displayContract(value: string): string {
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
    autonomous_trading_blueprint_status: "自治大纲状态契约",
    agent_authority_status: "Agent 权责状态契约",
    agent_scorecard_readiness: "Agent 评分契约",
    agent_briefing_readiness: "Agent 简报契约",
    agent_chain_health: "Agent 链路健康契约",
    proposal_generation_context_coverage: "提案上下文覆盖契约",
    candidate_generation_context_coverage: "候选上下文覆盖契约",
    candidate_bridge_review_coverage: "候选桥接审查契约",
  };
  const base = normalized.replace(/\.\d+$/, "").replace(/\.v\d+$/, "");
  return labels[base] || value.replace(/^v\d+_/, "").replace(/\.v\d+$/, "").replaceAll("_", " ");
}

export function statusTone(value: string): Tone {
  const normalized = value.toLowerCase();
  if (["ok", "ready", "available", "healthy", "pass", "aligned", "locked", "fresh"].includes(normalized)) return "ok";
  if (["partial", "attention", "degraded", "warn", "warning", "empty", "unknown", "stale"].includes(normalized)) return "warn";
  if (["bad", "blocked", "failed", "error", "missing", "divergent"].includes(normalized)) return "bad";
  return "mute";
}

export function countOf(value: unknown): number {
  return Array.isArray(value) ? value.length : 0;
}

export function CompactFacts({ facts }: { facts: Array<{ label: string; value: string; tone?: Tone }> }) {
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

export function ChainStep({
  icon: Icon,
  label,
  status,
  detail,
  tone,
}: {
  icon: typeof Network;
  label: string;
  status: string;
  detail?: string;
  tone?: Tone;
}) {
  return (
    <div className={`v16-chain-step v16-chain-${tone || statusTone(status)}`}>
      <div className="v16-chain-icon">
        <Icon size={16} aria-hidden="true" />
      </div>
      <div>
        <strong>{label}</strong>
        <span>{detail || displayValue(status)}</span>
      </div>
      <StatusPill status={displayValue(status)} tone={tone || statusTone(status)} />
    </div>
  );
}

export function BlueprintOverview({
  blueprint,
  agentAuthority,
  proposalContext,
  candidateContext,
  candidateReview,
  chainHealth,
  liveAutonomy,
}: {
  blueprint: Record<string, unknown>;
  agentAuthority: Record<string, unknown>;
  proposalContext: Record<string, unknown>;
  candidateContext: Record<string, unknown>;
  candidateReview: Record<string, unknown>;
  chainHealth: Record<string, unknown>;
  liveAutonomy: Record<string, unknown>;
}) {
  const checks = pickArray(blueprint, ["checks"]);
  const blockers = pickArray(blueprint, ["blockers"]);
  const deviationGuard = asRecord(pick(blueprint, ["deviation_guard"]));
  const boundary = asRecord(pick(blueprint, ["boundary"]));
  const chainChecks = pickArray(chainHealth, ["checks"]);
  return (
    <>
      <div className="v16-blueprint-summary">
        <CompactMetric label="大纲" value={displayValue(pickString(blueprint, ["status"], "unknown"))} detail={`阻断 ${blockers.length}`} tone={pickBoolean(blueprint, ["ok"], false) ? "ok" : "warn"} />
        <CompactMetric label="Agent" value={formatDecimal(pickNumber(agentAuthority, ["registered_agents"], 0), 0)} detail={`${countOf(pick(agentAuthority, ["unknown_sources"]))} 未知 / ${countOf(pick(agentAuthority, ["contract_violations"]))} 违规`} tone={pickBoolean(agentAuthority, ["ok"], false) ? "ok" : "warn"} />
        <CompactMetric label="Proposal Context" value={displayValue(pickString(proposalContext, ["status"], "unknown"))} detail={`缺 ${formatDecimal(pickNumber(proposalContext, ["missing_required_context_count"], 0), 0)}`} tone={statusTone(pickString(proposalContext, ["status"], "unknown"))} />
        <CompactMetric label="Candidate Context" value={displayValue(pickString(candidateContext, ["status"], "unknown"))} detail={`缺 ${formatDecimal(pickNumber(candidateContext, ["missing_required_context_count"], 0), 0)}`} tone={statusTone(pickString(candidateContext, ["status"], "unknown"))} />
        <CompactMetric label="Bridge Review" value={displayValue(pickString(candidateReview, ["status"], "unknown"))} detail={`缺 ${formatDecimal(pickNumber(candidateReview, ["missing_required_review_count"], 0), 0)}`} tone={statusTone(pickString(candidateReview, ["status"], "unknown"))} />
        <CompactMetric label="Feedback" value={displayValue(pickString(chainHealth, ["status"], "unknown"))} detail={`检查 ${chainChecks.length}`} tone={statusTone(pickString(chainHealth, ["status"], "unknown"))} />
      </div>

      <div className="v16-chain-map" aria-label="autonomy governance chain">
        <ChainStep icon={UsersRound} label="Agent" status={pickString(agentAuthority, ["status"], "unknown")} detail={`${formatDecimal(pickNumber(agentAuthority, ["registered_agents"], 0), 0)} registered`} />
        <ChainStep icon={Route} label="Proposal" status={pickString(proposalContext, ["status"], "unknown")} detail={`${formatDecimal(pickNumber(proposalContext, ["missing_required_context_count"], 0), 0)} missing`} />
        <ChainStep icon={ListChecks} label="Candidate" status={pickString(candidateReview, ["status"], "unknown")} detail={`${formatDecimal(pickNumber(candidateReview, ["candidate_bridge_count"], 0), 0)} bridges`} />
        <ChainStep icon={ShieldCheck} label="Policy Gate" status={pickString(blueprint, ["status"], "unknown")} detail={pickBoolean(deviationGuard, ["does_not_bypass_risk_policy"], false) ? "Risk/Decision kept" : "gate attention"} />
        <ChainStep icon={Activity} label="Execution" status={pickBoolean(deviationGuard, ["does_not_create_second_execution_path"], false) ? "ok" : "attention"} detail={pickString(liveAutonomy, ["autonomy_mode"], "manual")} />
        <ChainStep icon={BookOpenCheck} label="Memory" status={pickString(chainHealth, ["status"], "unknown")} detail={`lessons ${formatDecimal(pickNumber(chainHealth, ["trade_feedback_summary.lesson_count"], 0), 0)}`} />
      </div>

      <div className="v16-blueprint-checks">
        {checks.slice(0, 9).map((raw, index) => {
          const item = asRecord(raw);
          const component = pickString(item, ["component"], `check_${index}`);
          const ok = pickBoolean(item, ["ok"], false);
          return (
            <div className="v16-check-row" key={`${component}-${index}`}>
              {ok ? <CheckCircle2 size={16} aria-hidden="true" /> : <AlertTriangle size={16} aria-hidden="true" />}
              <div>
                <strong>{displayValue(component)}</strong>
                <span>{displayValue(pickString(item, ["status"], ok ? "ok" : "attention"))}</span>
              </div>
              <StatusPill status={ok ? "OK" : "Attention"} tone={ok ? "ok" : "warn"} />
            </div>
          );
        })}
      </div>

      <div className="v16-boundary v16-boundary-tight">
        <Field label="不扩权" value={pickBoolean(deviationGuard, ["does_not_expand_agent_authority"], false) ? "true" : "false"} tone={boolTone(pickBoolean(deviationGuard, ["does_not_expand_agent_authority"], false))} />
        <Field label="不绕风控" value={pickBoolean(deviationGuard, ["does_not_bypass_risk_policy"], false) ? "true" : "false"} tone={boolTone(pickBoolean(deviationGuard, ["does_not_bypass_risk_policy"], false))} />
        <Field label="不建第二执行路" value={pickBoolean(deviationGuard, ["does_not_create_second_execution_path"], false) ? "true" : "false"} tone={boolTone(pickBoolean(deviationGuard, ["does_not_create_second_execution_path"], false))} />
        <Field label="只读对齐" value={pickBoolean(boundary, ["read_only_alignment_status"], false) ? "true" : "false"} tone={boolTone(pickBoolean(boundary, ["read_only_alignment_status"], false))} />
      </div>
    </>
  );
}

export function AgentAuthorityPanel({
  agentAuthority,
  agentScorecard,
  agentBriefing,
  chainHealth,
}: {
  agentAuthority: Record<string, unknown>;
  agentScorecard: Record<string, unknown>;
  agentBriefing: Record<string, unknown>;
  chainHealth: Record<string, unknown>;
}) {
  const unknownSources = pickArray(agentAuthority, ["unknown_sources"]);
  const contractViolations = pickArray(agentAuthority, ["contract_violations"]);
  const topAgents = pickArray(agentScorecard, ["top_agents"]);
  const summary = asRecord(pick(agentScorecard, ["summary"]));
  const reviewRules = asRecord(pick(agentBriefing, ["review_rules"]));
  return (
    <>
      <div className="v16-agent-strip">
        <CompactMetric label="登记 Agent" value={formatDecimal(pickNumber(agentAuthority, ["registered_agents"], 0), 0)} detail={displayContract(pickString(agentAuthority, ["schema_version"], "--"))} tone={pickBoolean(agentAuthority, ["ok"], false) ? "ok" : "warn"} />
        <CompactMetric label="未知来源" value={formatDecimal(unknownSources.length, 0)} detail="review only" tone={unknownSources.length ? "warn" : "ok"} />
        <CompactMetric label="合同违规" value={formatDecimal(contractViolations.length, 0)} detail={displayValue(pickString(chainHealth, ["status"], "unknown"))} tone={contractViolations.length ? "bad" : "ok"} />
        <CompactMetric label="提案流" value={formatDecimal(pickNumber(summary, ["proposal_count"], 0), 0)} detail={`候选 ${formatDecimal(pickNumber(summary, ["candidate_count"], 0), 0)}`} tone={pickNumber(summary, ["proposal_count"], 0) || pickNumber(summary, ["candidate_count"], 0) ? "ok" : "warn"} />
      </div>

      <div className="v16-agent-list">
        {(topAgents.length ? topAgents : [
          { source_agent: "v16_brain", authority_state: "review_only" },
          { source_agent: "autonomous_learning", authority_state: "review_only" },
          { source_agent: "factor_governance", authority_state: "review_only" },
          { source_agent: "factor_pruning_governance", authority_state: "review_only" },
          { source_agent: "llm_advisory", authority_state: "advisory_only" },
          { source_agent: "lightgbm_shadow_models", authority_state: "review_only" },
        ]).slice(0, 6).map((raw, index) => {
          const item = asRecord(raw);
          const sourceAgent = pickString(item, ["source_agent"], `agent_${index}`);
          const applied = pickNumber(item, ["applied_count"], pickNumber(item, ["application_count"], 0));
          const proposals = pickNumber(item, ["proposal_count"], 0);
          const state = pickString(item, ["authority_state"], pickString(item, ["required_gate"], "review_only"));
          return (
            <div className="v16-agent-row" key={`${sourceAgent}-${index}`}>
              <UsersRound size={16} aria-hidden="true" />
              <div>
                <strong>{displayValue(sourceAgent)}</strong>
                <span>{formatDecimal(proposals, 0)} proposals / {formatDecimal(applied, 0)} applied</span>
              </div>
              <StatusPill status={displayValue(state)} tone={state === "advisory_only" ? "warn" : state.includes("blocked") ? "bad" : "mute"} />
            </div>
          );
        })}
      </div>

      <div className="v16-boundary v16-boundary-tight">
        <Field label="高影响需审查" value={pickBoolean(reviewRules, ["high_impact_requires_review"], true) ? "true" : "false"} tone={boolTone(pickBoolean(reviewRules, ["high_impact_requires_review"], true))} />
        <Field label="低可靠加严" value={pickBoolean(reviewRules, ["low_reliability_requires_extra_evidence"], true) ? "true" : "false"} tone={boolTone(pickBoolean(reviewRules, ["low_reliability_requires_extra_evidence"], true))} />
        <Field label="链路" value={displayValue(pickString(chainHealth, ["status"], "unknown"))} tone={statusTone(pickString(chainHealth, ["status"], "unknown"))} />
        <Field label="契约" value={displayContract(pickString(agentBriefing, ["schema_version"], "--"))} />
      </div>
    </>
  );
}

export function CoveragePanel({
  proposalContext,
  candidateContext,
  candidateReview,
}: {
  proposalContext: Record<string, unknown>;
  candidateContext: Record<string, unknown>;
  candidateReview: Record<string, unknown>;
}) {
  const items = [
    { key: "proposal", label: "Proposal Context", record: proposalContext, missing: "missing_required_context_count", legacy: "legacy_missing_context_count", total: "policy_suggestion_count" },
    { key: "candidate", label: "Candidate Context", record: candidateContext, missing: "missing_required_context_count", legacy: "legacy_missing_context_count", total: "candidate_count" },
    { key: "bridge", label: "Bridge Review", record: candidateReview, missing: "missing_required_review_count", legacy: "legacy_unreviewed_count", total: "candidate_bridge_count" },
  ];
  return (
    <div className="v16-coverage-list">
      {items.map((item) => {
        const status = pickString(item.record, ["status"], "unknown");
        return (
          <div className="v16-coverage-row" key={item.key}>
            <div>
              <strong>{item.label}</strong>
              <span>{displayContract(pickString(item.record, ["schema_version"], "--"))}</span>
            </div>
            <CompactFacts facts={[
              { label: "状态", value: displayValue(status), tone: statusTone(status) },
              { label: "总数", value: formatDecimal(pickNumber(item.record, [item.total], 0), 0), tone: "mute" },
              { label: "缺失", value: formatDecimal(pickNumber(item.record, [item.missing], 0), 0), tone: pickNumber(item.record, [item.missing], 0) ? "bad" : "ok" },
              { label: "历史", value: formatDecimal(pickNumber(item.record, [item.legacy], 0), 0), tone: pickNumber(item.record, [item.legacy], 0) ? "warn" : "mute" },
            ]} />
          </div>
        );
      })}
    </div>
  );
}

export function MemoryList({ items, empty }: { items: unknown[]; empty: string }) {
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

export function HypothesisList({ items }: { items: unknown[] }) {
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

export function ActionPlanList({ items }: { items: unknown[] }) {
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

export function EvaluationList({ items }: { items: unknown[] }) {
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

export function ExecutionList({ items }: { items: unknown[] }) {
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

export function GovernanceList({ items }: { items: unknown[] }) {
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

export function CandidateReviewList({ items }: { items: unknown[] }) {
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

export function GuardrailList({ items }: { items: unknown[] }) {
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

export function ProposalRegistryList({
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

