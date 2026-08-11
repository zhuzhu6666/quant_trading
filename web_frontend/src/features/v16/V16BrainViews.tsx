import {
  ArrowRight,
  CircleDot,
  Clock3,
  Network,
  UsersRound,
} from "lucide-react";
import { CompactMetric, Field, type Tone } from "@/components/DashboardBits";
import { StatusPill } from "@/components/StatusPill";
import type { FactState } from "@/api/fact";
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

export function pickString(value: unknown, path: string[], fallback = ""): string {
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

type PipelineFact = { state: FactState; failed: boolean };

export function formatTime(raw: unknown): string {
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) return "";
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
    v16_phase1_read_only_brain: "系统态势只读汇总",
    v16_phase2_shadow_brain: "只观察计划",
    v16_phase2_shadow_brain_eval: "后验评价",
    v16_phase3_low_impact_autonomous_brain: "低影响执行",
    v16_phase4_medium_impact_governance: "治理候选",
    v16_phase5_live_ready_guardrails: "实盘护栏",
  };
  return labels[normalized] || value.replace(/^v\d+_/, "").replaceAll("_", " ");
}

export function displayAction(value: string): string {
  const labels: Record<string, string> = {
    shadow_supervisor_template_review: "持仓监督模板复核",
    shadow_factor_weight_review: "因子权重复核",
    shadow_context_policy_review: "场景策略复核",
    shadow_parameter_template_review: "参数模板复核",
    run_replay_job: "只读回放",
    update_weight: "权重候选",
    switch_parameter_template: "参数模板候选",
    enable_context_policy: "场景策略候选",
    switch_position_supervisor_template: "持仓监督模板候选",
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
    bridge_ready: "可交接",
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
    not_bridge_compatible: "暂不可交接",
    no_new_risk: "不增风险",
    none_shadow_only: "只观察，不影响交易",
    normal: "正常",
    observation_only: "仅观察",
    ok: "正常",
    only_close: "仅平仓",
    pass: "通过",
    positive: "正面",
    posterior: "后验",
    proposal_registry: "治理提案总线",
    partial: "部分对齐",
    request_review: "请求审查",
    request_replay: "请求回放",
    ready: "就绪",
    reject: "拒绝",
    revoked: "已撤销",
    reviewed: "已审查",
    shadow: "只观察",
    shadow_recorded: "只观察记录已保存",
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
    demo_nursery: "Demo 自动演化",
    advisory_only: "只建议",
    review_only: "只审查",
    requires_control_gate: "需通过治理审核",
    no_execution_authority: "无执行权限",
    blocked_by_agent_authority: "权限阻断",
    world_model: "系统态势摘要",
    critic: "证据审查",
    agent_authority: "智能体权限",
    risk_policy: "风险策略",
    decision_policy: "决策规则",
    runtime_config: "运行配置",
    coordinator: "治理事务提交器",
    v16_brain: "自治治理中枢",
    autonomous_learning: "自主学习",
    factor_governance: "因子治理",
    factor_pruning_governance: "因子裁剪治理",
    position_supervisor_governance: "持仓监督治理",
    llm_advisory: "LLM 建议",
    lightgbm_shadow_models: "LightGBM 只观察模型",
    demo_nursery_learning_scope: "Demo 自动学习范围",
    agent_authority_contract: "智能体权限规则",
    proposal_generation_context: "提案上下文",
    candidate_generation_context: "候选上下文",
    candidate_bridge_review: "候选交接审查",
    proposal_registry_read_model: "治理提案汇总",
    memory_and_scorecard_feedback: "记忆与评分反馈",
    single_execution_boundary: "唯一执行边界",
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
    v15_readiness_contract: "运行治理就绪契约",
    v16_readiness_contract: "自治治理就绪契约",
    brain_state_snapshot: "自治状态快照",
    brain_memory_retrieval: "证据记忆检索",
    brain_action_plan_run: "只观察计划",
    brain_action_plan_eval_run: "后验评价契约",
    brain_low_impact_execution_run: "低影响执行契约",
    brain_medium_impact_governance_run: "治理候选契约",
    brain_governance_candidate_review_list: "候选审查契约",
    brain_governance_candidate_review_run: "候选审查契约",
    brain_live_ready_guardrail: "实盘护栏契约",
    proposal_registry_list: "治理提案列表",
    proposal_registry_status: "治理提案状态",
    live_autonomy_status: "实盘自治契约",
    autonomous_trading_blueprint_status: "自治大纲状态契约",
    agent_authority_status: "智能体权限状态",
    agent_scorecard_readiness: "智能体质量评分",
    agent_briefing_readiness: "智能体运行简报",
    agent_chain_health: "智能体链路健康",
    proposal_generation_context_coverage: "提案上下文覆盖契约",
    candidate_generation_context_coverage: "候选上下文覆盖契约",
    candidate_bridge_review_coverage: "候选交接审查",
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

export type RuntimeLogEntry = {
  icon: typeof Network;
  label: string;
  status: string;
  conclusion: string;
  reason: string;
  next: string;
  evidence: string;
  tone: Tone;
};

export function RuntimeLog({ rows }: { rows: RuntimeLogEntry[] }) {
  if (!rows.length) return <div className="empty-state-small">暂无运行日志</div>;
  return (
    <div className="v16-runtime-log" aria-label="自治治理运行日志">
      {rows.map((row, index) => {
        const Icon = row.icon;
        return (
          <article className="v16-runtime-row" key={`${row.label}-${index}`}>
            <div className="v16-runtime-stage">
              <span className="v16-runtime-index">{index + 1}</span>
              <Icon size={15} aria-hidden="true" />
              <strong>{row.label}</strong>
            </div>
            <div className="v16-runtime-cell v16-runtime-conclusion">
              <span>结论</span>
              <strong>{row.conclusion}</strong>
            </div>
            <div className="v16-runtime-cell v16-runtime-reason">
              <span>原因 / 依据</span>
              <strong>{row.reason}</strong>
            </div>
            <div className="v16-runtime-cell v16-runtime-next">
              <span>下一步</span>
              <strong>{row.next}</strong>
            </div>
            <div className="v16-runtime-status">
              <StatusPill status={row.status} tone={row.tone} />
            </div>
            <div className="v16-runtime-evidence"><Clock3 size={12} aria-hidden="true" />{row.evidence}</div>
          </article>
        );
      })}
    </div>
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
  const agents = pickArray(agentScorecard, ["agents"]);
  const summary = asRecord(pick(agentScorecard, ["summary"]));
  const reviewRules = asRecord(pick(agentBriefing, ["review_rules"]));
  return (
    <>
      <div className="v16-agent-strip">
        <CompactMetric label="登记智能体" value={formatDecimal(pickNumber(agentAuthority, ["registered_agents"], 0), 0)} detail={displayContract(pickString(agentAuthority, ["schema_version"], ""))} tone={pickBoolean(agentAuthority, ["ok"], false) ? "ok" : "warn"} />
        <CompactMetric label="未知来源" value={formatDecimal(unknownSources.length, 0)} detail="仅供审查" tone={unknownSources.length ? "warn" : "ok"} />
        <CompactMetric label="权限违规" value={formatDecimal(contractViolations.length, 0)} detail={displayValue(pickString(chainHealth, ["status"], ""))} tone={contractViolations.length ? "bad" : "ok"} />
        <CompactMetric label="近期治理记录" value={formatDecimal(pickNumber(summary, ["proposal_count"], 0), 0)} detail={`候选 ${formatDecimal(pickNumber(summary, ["candidate_count"], 0), 0)} · 最多统计最近 300 条`} tone={pickNumber(summary, ["proposal_count"], 0) || pickNumber(summary, ["candidate_count"], 0) ? "ok" : "warn"} />
      </div>

      <div className="v16-agent-list">
        {agents.map((raw, index) => {
          const item = asRecord(raw);
          const sourceAgent = pickString(item, ["source_agent"], `agent_${index}`);
          const proposals = pickNumber(item, ["proposal_count"], 0);
          const candidates = pickNumber(item, ["candidate_count"], 0);
          const suggestions = pickNumber(item, ["policy_suggestion_count"], 0);
          const observations = pickNumber(item, ["advisory_shadow_count"], 0);
          const applications = pickNumber(item, ["application_count"], 0);
          const applied = pickNumber(item, ["applied_application_count"], 0);
          const activity = [
            proposals ? `治理提案 ${formatDecimal(proposals, 0)}` : "",
            candidates ? `候选 ${formatDecimal(candidates, 0)}` : "",
            suggestions ? `策略建议 ${formatDecimal(suggestions, 0)}` : "",
            observations ? `观察记录 ${formatDecimal(observations, 0)}` : "",
            applications ? `治理处理 ${formatDecimal(applications, 0)}` : "",
            applied ? `实际应用 ${formatDecimal(applied, 0)}` : "",
          ].filter(Boolean);
          const state = pickString(item, ["authority_state"], pickString(item, ["required_gate"], "review_only"));
          return (
            <div className="v16-agent-row" key={`${sourceAgent}-${index}`}>
              <UsersRound size={16} aria-hidden="true" />
              <div>
                <strong>{displayValue(sourceAgent)}</strong>
                <span>{activity.length ? activity.join(" · ") : "尚未触发治理任务"}</span>
              </div>
              <StatusPill status={displayValue(state)} tone={state === "advisory_only" ? "warn" : state.includes("blocked") ? "bad" : "mute"} />
            </div>
          );
        })}
        {!agents.length ? <div className="empty-state">暂无智能体运行统计</div> : null}
      </div>

      <div className="v16-boundary v16-boundary-tight">
        <Field label="高影响需审查" value={pickBoolean(reviewRules, ["high_impact_requires_review"], true) ? "是" : "否"} tone={boolTone(pickBoolean(reviewRules, ["high_impact_requires_review"], true))} />
        <Field label="低可靠需补证据" value={pickBoolean(reviewRules, ["low_reliability_requires_extra_evidence"], true) ? "是" : "否"} tone={boolTone(pickBoolean(reviewRules, ["low_reliability_requires_extra_evidence"], true))} />
        <Field label="链路" value={displayValue(pickString(chainHealth, ["status"], ""))} tone={statusTone(pickString(chainHealth, ["status"], ""))} />
        <Field label="契约" value={displayContract(pickString(agentBriefing, ["schema_version"], ""))} />
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
    { key: "proposal", label: "提案上下文", record: proposalContext, missing: "missing_required_context_count", legacy: "legacy_missing_context_count", total: "policy_suggestion_count" },
    { key: "candidate", label: "候选上下文", record: candidateContext, missing: "missing_required_context_count", legacy: "legacy_missing_context_count", total: "candidate_count" },
    { key: "bridge", label: "交接审查", record: candidateReview, missing: "missing_required_review_count", legacy: "legacy_unreviewed_count", total: "candidate_bridge_count" },
  ];
  return (
    <div className="v16-coverage-list">
      {items.map((item) => {
        const status = pickString(item.record, ["status"], "");
        return (
          <div className="v16-coverage-row" key={item.key}>
            <div>
              <strong>{item.label}</strong>
              <span>{displayContract(pickString(item.record, ["schema_version"], ""))}</span>
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

export function GovernancePipeline({
  proposalItems,
  governanceItems,
  reviewItems,
  proposalFact,
  governanceFact,
  reviewFact,
  onReview,
  reviewing,
}: {
  proposalItems: unknown[];
  governanceItems: unknown[];
  reviewItems: unknown[];
  proposalFact: PipelineFact;
  governanceFact: PipelineFact;
  reviewFact: PipelineFact;
  onReview: (proposalId: string, route: string) => void;
  reviewing: boolean;
}) {
  type PipelineGroup = {
    refs: Set<string>;
    proposal?: Record<string, unknown>;
    governance?: Record<string, unknown>;
    review?: Record<string, unknown>;
    createdAt: number;
  };

  const groups: PipelineGroup[] = [];
  const add = (kind: "proposal" | "governance" | "review", raw: unknown, index: number) => {
    const item = asRecord(raw);
    const aliases = kind === "proposal"
      ? [
          pickString(item, ["proposal_id"]),
          pickString(item, ["source_ref_id"]),
          pickString(item, ["evidence_refs.candidate_id"]),
          pickString(item, ["evidence_refs.eval_id"]),
        ]
      : kind === "governance"
        ? [
            pickString(item, ["candidate_id"]),
            pickString(item, ["eval_id"]),
            pickString(item, ["plan_id"]),
            pickString(item, ["governance_id"]),
          ]
        : [pickString(item, ["candidate_id"]), pickString(item, ["review_id"])];
    const usableAliases = [...new Set(aliases.filter(Boolean))];
    const group = usableAliases.length
      ? groups.find((candidate) => usableAliases.some((alias) => candidate.refs.has(alias)))
      : undefined;
    const target = group || {
      refs: new Set<string>(),
      createdAt: pickNumber(item, ["updated_at"], pickNumber(item, ["created_at"], index)),
    };
    usableAliases.forEach((alias) => target.refs.add(alias));
    if (kind === "proposal") target.proposal = item;
    if (kind === "governance") target.governance = item;
    if (kind === "review") target.review = item;
    target.createdAt = Math.max(target.createdAt, pickNumber(item, ["updated_at"], pickNumber(item, ["created_at"], index)));
    if (!group) groups.push(target);
  };

  proposalItems.forEach((item, index) => add("proposal", item, index));
  governanceItems.forEach((item, index) => add("governance", item, index));
  reviewItems.forEach((item, index) => add("review", item, index));
  groups.sort((a, b) => b.createdAt - a.createdAt);

  const factIsUnavailable = (fact: PipelineFact): boolean => fact.failed || fact.state === "error";
  const factIsUnconfirmed = (fact: PipelineFact): boolean => fact.state === "unknown";

  const phase = (
    item: Record<string, unknown> | undefined,
    fact: PipelineFact,
    kind: "proposal" | "governance" | "review",
  ) => {
    if (factIsUnavailable(fact)) {
      return {
        label: "读取错误",
        tone: "bad" as Tone,
      };
    }
    if (factIsUnconfirmed(fact)) return { label: "待确认", tone: "pending" as Tone };
    if (!item) {
      return fact.state === "stale"
        ? { label: "未确认 · 数据已过期", tone: "stale" as Tone }
        : { label: "未形成", tone: "mute" as Tone };
    }
    const status = kind === "review"
      ? pickString(item, ["review_status"], "")
      : pickString(item, ["status"], "");
    const normalized = status.toLowerCase();
    const tone: Tone = kind === "review"
      ? pickBoolean(item, ["bridge_ready"], false)
        ? "ok"
        : normalized.includes("conflict")
          ? "bad"
          : "warn"
      : normalized === "reviewed" || normalized === "candidate_materialized"
        ? "ok"
        : normalized.includes("blocked") || normalized.includes("failed")
          ? "bad"
          : "warn";
    const label = displayValue(status || "已记录");
    return fact.state === "stale"
      ? { label: `${label} · 数据已过期`, tone: tone === "ok" ? "stale" as Tone : tone }
      : { label, tone };
  };

  const factBlocker = (fact: PipelineFact, item: Record<string, unknown> | undefined, label: string): string => {
    if (factIsUnavailable(fact)) return `${label}接口读取错误`;
    if (factIsUnconfirmed(fact)) return `${label}数据待确认`;
    if (fact.state === "stale" && !item) return `${label}数据已过期，无法确认`;
    return "";
  };

  const describeBlocker = (group: PipelineGroup): string => {
    // A governance row without candidate_id was not selected for bridging;
    // its missing review is not a review freshness failure. Only a concrete
    // candidate (or an existing review row) enters the review gate.
    const reviewRequired = Boolean(group.review) || Boolean(pickString(group.governance, ["candidate_id"]));
    const reviewDataBlocker = reviewRequired ? factBlocker(reviewFact, group.review, "审查") : "";
    if (reviewDataBlocker && (group.review || group.governance)) return reviewDataBlocker;
    const governanceDataBlocker = factBlocker(governanceFact, group.governance, "候选");
    if (governanceDataBlocker && (group.governance || group.proposal)) return governanceDataBlocker;
    const proposalDataBlocker = factBlocker(proposalFact, group.proposal, "提案");
    if (proposalDataBlocker && (group.proposal || group.governance || group.review)) return proposalDataBlocker;
    const review = group.review;
    if (review) {
      const conflict = asRecord(pick(review, ["conflict"]));
      const gaps = pickArray(review, ["evidence_gaps"]);
      if (pickBoolean(conflict, ["has_conflict"], pickBoolean(conflict, ["conflict"], false))) {
        return pickString(conflict, ["surface"], "存在候选冲突");
      }
      if (gaps.length) return `缺证据：${gaps.slice(0, 2).map(String).join("、")}`;
      if (!pickBoolean(review, ["bridge_ready"], false)) {
        return pickString(review, ["bridge_reason"], "候选交接未就绪");
      }
    }
    const governance = group.governance;
    if (governance) {
      const status = pickString(governance, ["status"], "");
      if (status.includes("blocked")) {
        const risk = asRecord(pick(governance, ["risk_verdict"]));
        return pickString(risk, ["reason"], displayValue(status));
      }
      if (!pickString(governance, ["candidate_id"])) return "候选尚未形成";
    }
    const proposal = group.proposal;
    if (proposal) {
      const conflict = asRecord(pick(proposal, ["conflict"]));
      if (pickBoolean(conflict, ["conflict"], false)) return pickString(conflict, ["severity"], "提案存在冲突");
      if (pickString(proposal, ["route_recommendation"]) === "request_review") return "提案等待人工审查";
    }
    if (!group.proposal) return "等待提案进入链路";
    if (!group.governance) return "提案已记录，尚未形成治理候选";
    if (!group.review) return "候选已形成，尚未完成交接审查";
    return "当前无阻断";
  };

  const nextStep = (group: PipelineGroup): string => {
    const proposal = group.proposal;
    const governance = group.governance;
    const review = group.review;
    if (factIsUnavailable(proposalFact)) return "先恢复提案接口";
    if (factIsUnconfirmed(proposalFact)) return "等待提案数据确认";
    if (proposalFact.state === "stale" && !proposal) return "刷新提案数据后确认";
    if (!proposal) return "等待提案形成";
    if (pickString(proposal, ["status"]) !== "reviewed") return "先记录提案审查";
    if (factIsUnavailable(governanceFact)) return "先恢复候选接口";
    if (factIsUnconfirmed(governanceFact)) return "等待候选数据确认";
    if (governanceFact.state === "stale" && !governance) return "刷新候选数据后确认";
    if (!governance) return "生成治理候选";
    if (!pickString(governance, ["candidate_id"])) return "修复候选生成阻断";
    if (factIsUnavailable(reviewFact)) return "先恢复审查接口";
    if (factIsUnconfirmed(reviewFact)) return "等待审查数据确认";
    if (reviewFact.state === "stale" && !review) return "刷新审查数据后确认";
    if (!review) return "运行候选交接审查";
    if (!pickBoolean(review, ["bridge_ready"], false)) return "补齐审查缺口";
    return "可交接；仍需治理审核";
  };

  if (!groups.length) {
    return (
      <div className="v16-pipeline-empty">
        <CircleDot size={16} aria-hidden="true" />
        <span>当前没有可关联的提案、候选或审查记录。先刷新或运行对应阶段。</span>
      </div>
    );
  }

  const blockedCount = groups.filter((group) => describeBlocker(group) !== "当前无阻断").length;
  const waitingCount = groups.filter((group) => nextStep(group).includes("等待")).length;
  const pipelineFacts = [
    ["提案", proposalFact],
    ["候选", governanceFact],
    ["审查", reviewFact],
  ] as const;
  const factStatus = pipelineFacts.map(([label, fact]) => {
    if (factIsUnavailable(fact)) return `${label}读取错误`;
    if (factIsUnconfirmed(fact)) return `${label}待确认`;
    if (fact.state === "stale") return `${label}已过期`;
    return `${label}已确认`;
  }).join(" · ");
  const factTone: Tone = pipelineFacts.some(([, fact]) => factIsUnavailable(fact))
    ? "bad"
    : pipelineFacts.some(([, fact]) => factIsUnconfirmed(fact))
      ? "pending"
      : pipelineFacts.some(([, fact]) => fact.state === "stale")
        ? "stale"
        : "ok";
  return (
    <div className="v16-pipeline">
      <div className={`v16-pipeline-fact-note v16-pipeline-fact-note-${factTone}`}>
        <strong>账本状态</strong>
        <span>{factStatus}</span>
        <small>{factTone === "stale" ? "保留已读取记录，但不把过期记录当作当前确认" : factTone === "pending" ? "没有可验证观测，不能推断阶段是否已完成" : factTone === "bad" ? "接口读取失败，不能用缓存代替当前事实" : "三段账本均有当前观测"}</small>
      </div>
      <div className="v16-pipeline-summary">
        <CompactMetric label="合并对象" value={formatDecimal(groups.length, 0)} detail="同一对象只保留一行" tone="mute" />
        <CompactMetric label="当前阻断" value={formatDecimal(blockedCount, 0)} detail="按最具体阶段归因" tone={blockedCount ? "warn" : "ok"} />
        <CompactMetric label="等待形成" value={formatDecimal(waitingCount, 0)} detail="尚未进入下一阶段" tone={waitingCount ? "warn" : "ok"} />
        <CompactMetric label="阅读顺序" value="提案 → 候选 → 审查" detail="不代表已授权执行" tone="mute" />
      </div>
      <div className="v16-pipeline-list" aria-label="待治理链路日志">
        {groups.map((group, index) => {
          const proposal = group.proposal;
          const governance = group.governance;
          const review = group.review;
          const proposalPhase = phase(proposal, proposalFact, "proposal");
          const governancePhase = phase(governance, governanceFact, "governance");
          const reviewRequired = Boolean(review) || Boolean(pickString(governance, ["candidate_id"]));
          const reviewPhase = reviewRequired
            ? phase(review, reviewFact, "review")
            : { label: "不适用", tone: "mute" as Tone };
          const subject = pickString(governance, ["scope_key"])
            || pickString(review, ["candidate.scope_key"])
            || pickString(proposal, ["target_scope"])
            || "待关联治理对象";
          const action = pickString(governance, ["governance_action"])
            || pickString(review, ["candidate.action"])
            || pickString(proposal, ["proposal_action"])
            || "治理记录";
          const blocker = describeBlocker(group);
          const proposalId = pickString(proposal, ["proposal_id"]);
          const route = pickString(proposal, ["route_recommendation"], "observe");
          const ids = [
            proposalId ? `提案 ${proposalId}` : "",
            pickString(governance, ["candidate_id"]) ? `候选 ${pickString(governance, ["candidate_id"])}` : "",
            pickString(review, ["review_id"]) ? `审查 ${pickString(review, ["review_id"])}` : "",
          ].filter(Boolean);
          return (
            <article className="v16-pipeline-row" key={`${Array.from(group.refs).join("|") || "pipeline"}-${index}`}>
              <div className="v16-pipeline-object">
                <span>治理对象</span>
                <strong>{displayAction(action)}</strong>
                <small title={subject}>{subject}</small>
              </div>
              <div className="v16-pipeline-stages">
                {[{ label: "提案", value: proposalPhase }, { label: "候选", value: governancePhase }, { label: "审查", value: reviewPhase }].map((stage, stageIndex) => (
                  <div className="v16-pipeline-stage-wrap" key={stage.label}>
                    <div className="v16-pipeline-stage">
                      <span>{stage.label}</span>
                      <StatusPill status={stage.value.label} tone={stage.value.tone} />
                    </div>
                    {stageIndex < 2 ? <ArrowRight size={13} aria-hidden="true" /> : null}
                  </div>
                ))}
              </div>
              <div className={`v16-pipeline-blocker ${blocker === "当前无阻断" ? "v16-pipeline-blocker-clear" : ""}`.trim()}>
                <span>当前停点</span>
                <strong>{blocker}</strong>
              </div>
              <div className="v16-pipeline-next">
                <span>下一步</span>
                <strong>{nextStep(group)}</strong>
              </div>
              <div className="v16-pipeline-meta">
                <div className="brain-ref-row">{ids.map((id) => <span key={id}>{id}</span>)}</div>
                <small><Clock3 size={12} aria-hidden="true" />{formatTime(group.createdAt) || "时间未提供"}</small>
                {proposal && pickString(proposal, ["status"]) !== "reviewed" ? (
                  <button className="brain-inline-button" type="button" disabled={!proposalId || reviewing} onClick={() => onReview(proposalId, route)}>
                    {reviewing ? "记录中" : "记录审查"}
                  </button>
                ) : null}
              </div>
            </article>
          );
        })}
      </div>
    </div>
  );
}

export function MemoryList({ items, empty }: { items: unknown[]; empty: string }) {
  if (!items.length) return <div className="empty-state-small">{empty}</div>;
  return (
    <div className="brain-list">
      {items.slice(0, 8).map((raw, index) => {
        const item = asRecord(raw);
        const source = pickString(item, ["source_table"], "memory");
        const sourceId = pickString(item, ["source_id"], "");
        const polarity = pickString(item, ["polarity"], "neutral");
        const summary = pickString(item, ["text_summary"], source);
        const meta = `${source} · 证据分 ${formatDecimal(pickNumber(item, ["evidence_score"], 0), 2)} · 相似度 ${formatDecimal(pickNumber(item, ["similarity_score"], 0), 2)}`;
        return (
          <div className="brain-list-row brain-memory-row" key={`${source}-${sourceId}-${index}`}>
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
  if (!items.length) return <div className="empty-state-small">暂无假设</div>;
  return (
    <div className="brain-hypothesis-list">
      {items.map((raw, index) => {
        const item = asRecord(raw);
        const risk = pickString(item, ["risk_class"], "");
        return (
          <article className="brain-hypothesis" key={`${pickString(item, ["hypothesis_id"], "hyp")}-${index}`}>
            <div className="brain-hypothesis-head">
              <div>
                <strong>{pickString(item, ["scope"], "scope")}</strong>
                <span>{pickString(item, ["claim"], "")}</span>
              </div>
              <StatusPill status={displayValue(risk)} tone={riskTone(risk)} />
            </div>
            <div className="brain-mini-grid brain-mini-grid-tight">
              <CompactMetric label="置信度" value={`${formatDecimal(scorePct(pickNumber(item, ["confidence"], 0)), 1)}%`} tone="mute" />
              <CompactMetric label="证据分" value={`${formatDecimal(scorePct(pickNumber(item, ["evidence_score"], 0)), 1)}%`} tone={pickNumber(item, ["evidence_score"], 0) >= 0.5 ? "ok" : "warn"} />
              <CompactMetric label="动作范围" value={displayValue(pickString(item, ["action_scope"], "observe_only"))} tone="warn" />
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
  if (!items.length) return <div className="empty-state-small">暂无只观察计划</div>;
  return (
    <div className="brain-action-plan-list">
      {items.slice(0, 8).map((raw, index) => {
        const item = asRecord(raw);
        const scope = asRecord(pick(item, ["scope"]));
        const status = pickString(item, ["status"], "");
        const verdict = pickString(item, ["critic_verdict"], "");
        const risk = pickString(item, ["risk_class"], "");
        const requiredServices = pickArray(item, ["required_services"]).map(String);
        return (
          <article className="brain-action-plan brain-action-plan-compact" key={`${pickString(item, ["plan_id"], "plan")}-${index}`}>
            <div className="brain-hypothesis-head">
              <div>
                <strong>{displayAction(pickString(item, ["action_type"], "action_plan"))}</strong>
                <span>
                  {pickString(scope, ["scope_type"], "scope")} · {pickString(scope, ["scope_key"], "")}
                </span>
              </div>
              <StatusPill status={displayValue(status)} tone={status === "shadow_recorded" ? "ok" : "warn"} />
            </div>
            <CompactFacts facts={[
              { label: "证据审查", value: displayValue(verdict), tone: verdict === "pass" ? "ok" : verdict === "reject" ? "bad" : "warn" },
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
        const verdict = pickString(item, ["comparison_verdict"], "");
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
              { label: "交易回放", value: pickBoolean(presence, ["replay_report"], false) ? "有" : "无", tone: boolTone(pickBoolean(presence, ["replay_report"], false)) },
              { label: "交易结果", value: pickBoolean(presence, ["trade_outcome_review"], false) ? "有" : "无", tone: boolTone(pickBoolean(presence, ["trade_outcome_review"], false)) },
              { label: "持仓监督", value: pickBoolean(presence, ["position_supervisor_trace"], false) ? "有" : "无", tone: boolTone(pickBoolean(presence, ["position_supervisor_trace"], false)) },
            ]} />
            <div className="brain-ref-row">
              <span>奖励变化 {formatDecimal(pickNumber(comparison, ["learning_effects.avg_delta_reward"], 0), 3)}</span>
              <span>平均盈亏 {formatDecimal(pickNumber(comparison, ["trade_outcomes.avg_pnl"], 0), 2)}</span>
              <span>回放一致率 {formatDecimal(scorePct(pickNumber(comparison, ["replay.agreement"], 0)), 1)}%</span>
            </div>
          </article>
        );
      })}
    </div>
  );
}

export function ExecutionList({ items }: { items: unknown[] }) {
  if (!items.length) return <div className="empty-state-small">暂无低影响执行</div>;
  return (
    <div className="brain-action-plan-list">
      {items.slice(0, 8).map((raw, index) => {
        const item = asRecord(raw);
        const status = pickString(item, ["status"], "");
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
              { label: "风险检查", value: displayValue(pickBoolean(riskVerdict, ["allowed"], false) ? "allow" : "block"), tone: boolTone(pickBoolean(riskVerdict, ["allowed"], false)) },
              { label: "证据审查", value: displayValue(pickString(item, ["critic_verdict"], "")), tone: pickString(item, ["critic_verdict"], "") === "reject" ? "bad" : "warn" },
              { label: "后验", value: displayValue(pickString(item, ["comparison_verdict"], "")), tone: pickString(item, ["comparison_verdict"], "") === "caution" ? "warn" : "ok" },
            ]} />
            <div className="brain-ref-row">
              <span>{pickString(result, ["replay_run_id"], "暂无回放")}</span>
              <span>后验异常 {pickBoolean(posterior, ["bad_posterior"], false) ? "是" : "否"}</span>
              <span>{pickString(riskVerdict, ["reason"], "")}</span>
            </div>
          </article>
        );
      })}
    </div>
  );
}

export function GuardrailList({ items }: { items: unknown[] }) {
  if (!items.length) return <div className="empty-state-small">暂无实盘护栏</div>;
  return (
    <div className="brain-action-plan-list">
      {items.slice(0, 8).map((raw, index) => {
        const item = asRecord(raw);
        const status = pickString(item, ["status"], "");
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
                <span>{formatTime(pick(item, ["created_at"]))} · {displayValue(pickString(recommendation, ["target_mode"], ""))}</span>
              </div>
              <StatusPill status={displayValue(status)} tone={locked ? "ok" : status.includes("attention") ? "warn" : "mute"} />
            </div>
            <CompactFacts facts={[
              { label: "能力", value: displayValue(locked ? "locked" : "blocked"), tone: locked ? "ok" : "warn" },
              { label: "偏差", value: displayValue(pickString(divergence, ["status"], "")), tone: divergent ? "bad" : "ok" },
              { label: "事故", value: displayValue(pickString(incident, ["mode"], "normal")), tone: pickString(incident, ["mode"], "normal") === "normal" ? "ok" : "warn" },
              { label: "回滚", value: displayValue(pickBoolean(rollback, ["rollback_ready"], false) ? "ready" : "missing"), tone: boolTone(pickBoolean(rollback, ["rollback_ready"], false)) },
            ]} />
            <div className="brain-ref-row">
              <span>broker {pickString(divergence, ["broker_open_count"], "")}</span>
              <span>local {pickString(divergence, ["local_open_count"], "")}</span>
              <span>{pickArray(recommendation, ["reasons"]).slice(0, 2).map(String).join(", ") || "no blocker"}</span>
            </div>
          </article>
        );
      })}
    </div>
  );
}
