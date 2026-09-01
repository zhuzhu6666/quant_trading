import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  ArrowUp,
  ArrowRight,
  CheckCircle2,
  Clock3,
  Database,
  Gauge,
  GitBranch,
  Layers3,
  ListChecks,
  Radio,
  Server,
  ShieldCheck,
  Sparkles,
  Workflow as WorkflowIcon,
  X,
} from "lucide-react";
import type { FactEnvelope, FactState } from "@/api/fact";
import { epochSeconds, formatObservedTime } from "@/api/time";
import { getLearningLoopData } from "@/api/domains/learning";
import { getReadinessView } from "@/api/domains/ops";
import { Dialog, DialogClose, DialogSurface, DialogTitle, FactBadge, Panel } from "@/design-system/primitives";
import { useLiveState } from "@/hooks/useLiveState";
import type {
  GovernanceRecord,
  LearningLoopData,
  ReadinessDimension,
  LiveStateSnapshot,
} from "@/types/contracts";
import { WorkspaceTitle } from "@/workspaces/WorkspaceBits";

type LearningStageDetail = { label: string; value: string };

type LearningStageStatus = "ready" | "blocked" | "waiting" | "unknown" | "stale" | "error";

type LearningStage = {
  id: string;
  sequence: number;
  title: string;
  eyebrow: string;
  role: string;
  description: string;
  source: string;
  observedAt: string | number | null;
  state: FactState;
  status: LearningStageStatus;
  reasonCode: string | null;
  metric: string;
  details: LearningStageDetail[];
  fact: FactEnvelope;
  icon: typeof Activity;
};

type ArchitectureNodeStatus = "active" | "observed" | "structural" | "blocked" | "waiting" | "unknown" | "stale" | "error";

type ArchitectureNode = {
  id: string;
  title: string;
  eyebrow: string;
  role: string;
  description: string;
  source: string;
  observedAt: string | number | null;
  reasonCode: string | null;
  metric: string;
  input: string;
  output: string;
  status: ArchitectureNodeStatus;
  statusLabel: string;
  fact?: FactEnvelope;
  icon: typeof Activity;
};

type ArchitectureNodeDraft = Omit<ArchitectureNode, "status" | "statusLabel" | "observedAt" | "reasonCode" | "input" | "output"> & {
  observedAt?: string | number | null;
  reasonCode?: string | null;
  input?: string;
  output?: string;
  status?: ArchitectureNodeStatus;
  statusLabel?: string;
};

type ArchitectureLane = {
  id: string;
  title: string;
  eyebrow: string;
  note: string;
  edgeLabels: string[];
  nodes: ArchitectureNode[];
  active: boolean;
};

type ArchitectureMapData = {
  lanes: ArchitectureLane[];
  nodes: ArchitectureNode[];
};

function unknownFact(contract: string, reasonCode: string, state: FactState = "unknown"): FactEnvelope {
  return { envelope: "fact.v1", contract, state, source: "none", observed_at: null, generated_at: null, stale_after_sec: 0, reason_code: reasonCode, components: {} };
}

function dimensionFor(dimensions: ReadinessDimension[] | undefined, name: string): ReadinessDimension | undefined {
  return dimensions?.find((dimension) => dimension.name === name);
}

function resolvedLearningStatus(
  fact: FactEnvelope,
  options: { ready?: boolean; blocked?: boolean; waiting?: boolean; requestFailed?: boolean } = {},
): LearningStageStatus {
  if (options.requestFailed || fact.state === "error") return "error";
  if (fact.state === "stale") return "stale";
  if (fact.state !== "known") return "unknown";
  if (options.blocked) return "blocked";
  if (options.ready) return "ready";
  if (options.waiting) return "waiting";
  return "unknown";
}

function latestObservedAt(...facts: FactEnvelope[]): string | number | null {
  return facts
    .map((fact) => ({ value: fact.observed_at, epoch: epochSeconds(fact.observed_at) }))
    .sort((left, right) => right.epoch - left.epoch)[0]?.value ?? null;
}

function countLabel(fact: FactEnvelope, count: number): string {
  return fact.state === "error" || fact.state === "unknown" ? "—" : String(count);
}

function topCount(counts: Record<string, number>): string | null {
  return Object.entries(counts).sort((left, right) => right[1] - left[1])[0]?.[0] ?? null;
}

function statusCounts(items: Array<{ status: string }>): Record<string, number> {
  return items.reduce<Record<string, number>>((result, item) => {
    result[item.status] = (result[item.status] ?? 0) + 1;
    return result;
  }, {});
}

function governanceCount(items: GovernanceRecord[]): number {
  return items.length;
}

function emptyLearningLoopData(): LearningLoopData {
  const emptyList = <T,>(contract: string): { fact: FactEnvelope; items: T[]; count: number } => ({ fact: unknownFact(contract, "learning_loop_not_loaded"), items: [], count: 0 });
  const emptyGovernance = (contract: string) => ({ fact: unknownFact(contract, "learning_loop_not_loaded"), items: [] as GovernanceRecord[] });
  return {
    samples: emptyList("learning.autonomous-samples.v2"),
    reviews: emptyList("learning.summary.v2"),
    quality: { fact: unknownFact("learning.dataset-quality-health.v2", "learning_loop_not_loaded"), evidenceCounts: {}, evidenceExamples: [], entryContextStatus: null, openDecisions: 0, coverageRatio: {}, missingTotal: 0, maturedOpenOutcome: 0 },
    dataset: { fact: unknownFact("learning.dataset-readiness.v2", "learning_loop_not_loaded"), ready: null, level: null, thresholds: {}, quality: { trade: { total: 0, modelReady: 0, needsAttention: 0, readyRatio: null, avgQualityScore: null, missing: {} }, decision: { total: 0, modelReady: 0, needsAttention: 0, readyRatio: null, avgQualityScore: null, missing: {} } }, schemaIssueCount: 0, blockers: [], warnings: [] },
    shadowQueue: emptyList("learning.model-shadow-queue.v2"),
    inferenceAudits: emptyList("learning.model-inference-audits.v2"),
    suggestions: emptyList("learning.suggestions.v2"),
    governanceCandidates: emptyGovernance("ops.v16-governance-candidates.v2"),
    governanceReviews: emptyGovernance("ops.v16-governance-candidate-reviews.v2"),
    governanceProposals: emptyGovernance("ops.autonomy-proposals.v2"),
    applications: emptyList("learning.applications.v2"),
    effectQuality: null,
    effectQualityRequestFailed: false,
  };
}

function buildLearningStages(data: LearningLoopData, liveActive: boolean, liveFact?: FactEnvelope): LearningStage[] {
  const sampleStatus = statusCounts(data.samples.items.map((item) => ({ status: item.labelStatus })));
  const sampleReadyCount = data.samples.items.filter((item) => item.modelReady === true).length;
  const qualityBad = (data.quality.evidenceCounts.bad_total ?? 0) > 0 || data.quality.entryContextStatus === "degraded";
  const qualityChecked = data.quality.evidenceCounts.checked ?? 0;
  const datasetBlocked = data.dataset.schemaIssueCount > 0 || data.dataset.blockers.length > 0 || data.dataset.level === "not_ready";
  const datasetWaiting = data.dataset.ready !== true && data.dataset.level === "warming_up" && !datasetBlocked;
  const shadowStatus = statusCounts(data.shadowQueue.items);
  const shadowReady = data.shadowQueue.count > 0 || data.inferenceAudits.count > 0;
  const candidateCount = governanceCount(data.governanceCandidates.items);
  const reviewCount = governanceCount(data.governanceReviews.items);
  const proposalCount = governanceCount(data.governanceProposals.items);
  const governanceRequestFailed = [data.governanceCandidates.fact, data.governanceReviews.fact, data.governanceProposals.fact].some((fact) => fact.state === "error");
  const governanceReady = reviewCount > 0 || proposalCount > 0;
  const effectStatus = data.effectQuality?.status;
  const effectBlocked = effectStatus === "degraded" || (data.effectQuality?.retryCandidateCount ?? 0) > 0;
  const effectReady = effectStatus === "ok";
  const fallbackLiveFact = liveFact ?? unknownFact("live.loop.v2", "live_snapshot_missing");

  return [
    {
      id: "learning-input",
      sequence: 1,
      title: "决策与结果",
      eyebrow: "01 / 输入",
      role: "decision · supervisor · close review",
      description: "实时决策、持仓监督轨迹和已平仓结果是学习闭环的输入。这里用可见复盘记录确认结果链是否已经形成。",
      source: "state_v1 · /api/learning/summary",
      observedAt: data.reviews.fact.observed_at,
      state: data.reviews.fact.state,
      status: resolvedLearningStatus(data.reviews.fact, { ready: data.reviews.count > 0, waiting: data.reviews.count === 0 }),
      reasonCode: data.reviews.fact.reason_code ?? (data.reviews.count === 0 ? "no_trade_outcome_reviews" : null),
      metric: `${countLabel(data.reviews.fact, data.reviews.count)} 条复盘`,
      details: [
        { label: "最新结果", value: data.reviews.items[0]?.outcomeLabel ?? "暂无" },
        { label: "累计盈亏", value: data.reviews.items.filter((item) => item.pnl !== null).length ? `${data.reviews.items.filter((item) => item.pnl !== null).length} 条含 PnL` : "未提供" },
        { label: "下一步", value: data.reviews.count > 0 ? "物化为学习样本" : "等待平仓复盘" },
      ],
      fact: data.reviews.fact,
      icon: ListChecks,
    },
    {
      id: "learning-samples",
      sequence: 2,
      title: "学习样本",
      eyebrow: "02 / 物化",
      role: "autonomous_learning_sample",
      description: "把决策、监督和复盘事实统一物化为学习样本，并保留标签状态、完整性、训练权重和治理资格。",
      source: "state_v1 · /api/learning/autonomous/samples",
      observedAt: data.samples.fact.observed_at,
      state: data.samples.fact.state,
      status: resolvedLearningStatus(data.samples.fact, { ready: data.samples.count > 0, waiting: data.samples.count === 0 }),
      reasonCode: data.samples.fact.reason_code ?? (data.samples.count === 0 ? "no_learning_samples" : null),
      metric: `${countLabel(data.samples.fact, data.samples.count)} 条样本 · ${data.samples.fact.state === "known" ? `${sampleReadyCount} 条 model_ready` : "资格待确认"}`,
      details: [
        { label: "标签", value: Object.entries(sampleStatus).map(([key, value]) => `${key} ${value}`).join(" · ") || "暂无" },
        { label: "治理资格", value: data.samples.items.filter((item) => item.governanceEligible === true).length ? `${data.samples.items.filter((item) => item.governanceEligible === true).length} 条 eligible` : "暂无 eligible" },
        { label: "污染样本", value: data.samples.items.filter((item) => item.systemContaminated === true).length ? `${data.samples.items.filter((item) => item.systemContaminated === true).length} 条需隔离` : "未发现" },
      ],
      fact: data.samples.fact,
      icon: Layers3,
    },
    {
      id: "learning-evidence",
      sequence: 3,
      title: "证据合同",
      eyebrow: "03 / 资格",
      role: "integrity · causal · label · blockers",
      description: "证据合同决定样本能否进入监督训练或强治理。质量健康接口会检查资格自洽性、污染放行和入口上下文缺口。",
      source: "state_v1 · /api/learning/dataset/quality-health",
      observedAt: data.quality.fact.observed_at,
      state: data.quality.fact.state,
      status: resolvedLearningStatus(data.quality.fact, { ready: qualityChecked > 0 && !qualityBad, blocked: qualityBad, waiting: qualityChecked === 0 }),
      reasonCode: data.quality.fact.reason_code ?? (qualityBad ? topCount(data.quality.evidenceCounts) : qualityChecked === 0 ? "evidence_contract_not_observed" : null),
      metric: data.quality.fact.state === "error" || data.quality.fact.state === "unknown" ? "—" : `${qualityChecked} 条已检查 · ${data.quality.evidenceCounts.bad_total ?? 0} 条异常`,
      details: [
        { label: "合同异常", value: String(data.quality.evidenceCounts.bad_total ?? 0) },
        { label: "入口上下文", value: data.quality.entryContextStatus ?? "未提供" },
        { label: "主要问题", value: topCount(data.quality.evidenceCounts) ?? "无" },
      ],
      fact: data.quality.fact,
      icon: ShieldCheck,
    },
    {
      id: "learning-dataset",
      sequence: 4,
      title: "数据集准入",
      eyebrow: "04 / Gate",
      role: "trade + decision readiness",
      description: "数据集准入由服务端按 model_ready 数量、schema issue 和证据合同门槛判断。这里直接显示后端给出的 ready / warming_up / not_ready。",
      source: "state_v1 · /api/learning/dataset/readiness",
      observedAt: data.dataset.fact.observed_at,
      state: data.dataset.fact.state,
      status: resolvedLearningStatus(data.dataset.fact, { ready: data.dataset.ready === true, blocked: datasetBlocked, waiting: datasetWaiting }),
      reasonCode: data.dataset.fact.reason_code ?? data.dataset.blockers[0]?.code ?? (data.dataset.level ?? "dataset_readiness_unknown"),
      metric: data.dataset.level === "ready" ? "达到训练门槛" : data.dataset.level ?? "准入待确认",
      details: [
        { label: "交易样本", value: `${data.dataset.quality.trade.modelReady} / ${data.dataset.thresholds.min_ready_trades ?? "—"} ready` },
        { label: "决策样本", value: `${data.dataset.quality.decision.modelReady} / ${data.dataset.thresholds.min_ready_decisions ?? "—"} ready` },
        { label: "阻塞项", value: data.dataset.blockers[0]?.code ?? (data.dataset.schemaIssueCount ? `${data.dataset.schemaIssueCount} 个 schema issue` : "无") },
      ],
      fact: data.dataset.fact,
      icon: Database,
    },
    {
      id: "learning-shadow",
      sequence: 5,
      title: "模型影子",
      eyebrow: "05 / Shadow",
      role: "candidate queue · inference audit",
      description: "通过数据集准入后，模型仍先进入 shadow / advisory 阶段。候选队列和推理审计只用于观察，不直接拥有实盘执行权。",
      source: "model_registry · /api/learning/model/shadow-queue + inference",
      observedAt: latestObservedAt(data.shadowQueue.fact, data.inferenceAudits.fact),
      state: data.shadowQueue.fact.state === "error" || data.inferenceAudits.fact.state === "error" ? "error" : data.shadowQueue.fact.state,
      status: resolvedLearningStatus(data.shadowQueue.fact, { ready: shadowReady, waiting: !shadowReady }),
      reasonCode: data.shadowQueue.fact.reason_code ?? (shadowReady ? null : "no_shadow_model_observed"),
      metric: `${countLabel(data.shadowQueue.fact, data.shadowQueue.count)} 候选 · ${countLabel(data.inferenceAudits.fact, data.inferenceAudits.count)} 条审计`,
      details: [
        { label: "候选状态", value: Object.entries(shadowStatus).map(([key, value]) => `${key} ${value}`).join(" · ") || "暂无" },
        { label: "推理审计", value: String(data.inferenceAudits.count) },
        { label: "边界", value: "shadow / advisory only" },
      ],
      fact: data.shadowQueue.fact,
      icon: Sparkles,
    },
    {
      id: "learning-output",
      sequence: 6,
      title: "建议与因子输出",
      eyebrow: "06 / Advisory",
      role: "policy_suggestion · factor evidence",
      description: "模型和复盘结果汇入建议、因子证据和参数候选。建议是治理输入，不等于已经批准或已经修改运行时。",
      source: "state_v1 · /api/learning/suggestions",
      observedAt: data.suggestions.fact.observed_at,
      state: data.suggestions.fact.state,
      status: resolvedLearningStatus(data.suggestions.fact, { ready: data.suggestions.count > 0, waiting: data.suggestions.count === 0 }),
      reasonCode: data.suggestions.fact.reason_code ?? (data.suggestions.count === 0 ? "no_policy_suggestions" : null),
      metric: `${countLabel(data.suggestions.fact, data.suggestions.count)} 条建议`,
      details: [
        { label: "建议状态", value: Object.entries(statusCounts(data.suggestions.items)).map(([key, value]) => `${key} ${value}`).join(" · ") || "暂无" },
        { label: "最近动作", value: data.suggestions.items[0]?.action ?? "暂无" },
        { label: "最近因子", value: data.suggestions.items[0]?.factorId ?? "暂无" },
      ],
      fact: data.suggestions.fact,
      icon: WorkflowIcon,
    },
    {
      id: "learning-review",
      sequence: 7,
      title: "审查与协调",
      eyebrow: "07 / Coordinator",
      role: "candidate review · proposal registry",
      description: "治理候选进入 Coordinator / review / proposal registry。这里把候选、审查和提案数量并列展示，能看出是没有产出，还是卡在审查。",
      source: "state_v1 · /api/ops/brain/* + proposal registry",
      observedAt: latestObservedAt(data.governanceCandidates.fact, data.governanceReviews.fact, data.governanceProposals.fact),
      state: governanceRequestFailed ? "error" : data.governanceCandidates.fact.state,
      status: resolvedLearningStatus(data.governanceCandidates.fact, { ready: governanceReady, waiting: !governanceReady }),
      reasonCode: data.governanceCandidates.fact.reason_code ?? (candidateCount > 0 && reviewCount === 0 ? "candidate_waiting_review" : !governanceReady ? "governance_review_not_observed" : null),
      metric: `${candidateCount} 候选 · ${reviewCount} 审查 · ${proposalCount} 提案`,
      details: [
        { label: "候选", value: String(candidateCount) },
        { label: "审查", value: String(reviewCount) },
        { label: "提案", value: String(proposalCount) },
      ],
      fact: data.governanceCandidates.fact,
      icon: GitBranch,
    },
    {
      id: "learning-application",
      sequence: 8,
      title: "受控应用",
      eyebrow: "08 / Apply",
      role: "learning_application_log",
      description: "只有经过既有治理和风控边界的变更才会写入应用账本。应用状态和 scope 来自 state_v1，不在客户端判断是否应该写入。",
      source: "state_v1 · /api/learning/applications",
      observedAt: data.applications.fact.observed_at,
      state: data.applications.fact.state,
      status: resolvedLearningStatus(data.applications.fact, { ready: data.applications.count > 0, waiting: data.applications.count === 0 }),
      reasonCode: data.applications.fact.reason_code ?? (data.applications.count === 0 ? "no_learning_application" : null),
      metric: `${countLabel(data.applications.fact, data.applications.count)} 条应用`,
      details: [
        { label: "应用状态", value: Object.entries(statusCounts(data.applications.items)).map(([key, value]) => `${key} ${value}`).join(" · ") || "暂无" },
        { label: "最近 scope", value: data.applications.items[0]?.scope ?? "暂无" },
        { label: "最近动作", value: data.applications.items[0]?.action ?? "暂无" },
      ],
      fact: data.applications.fact,
      icon: CheckCircle2,
    },
    {
      id: "learning-effect",
      sequence: 9,
      title: "后验效果",
      eyebrow: "09 / Posterior",
      role: "effect ledger · rollback observation",
      description: "应用后的观察窗口、终态效果、归因质量和受控重试资格在这里收口。degraded 表示效果账本需要处理，不代表策略应该自动修改。",
      source: "state_v1 · /api/learning/effect-quality",
      observedAt: data.applications.fact.observed_at,
      state: data.applications.fact.state,
      status: resolvedLearningStatus(data.applications.fact, { ready: effectReady, blocked: effectBlocked || data.effectQuality?.status === "degraded", waiting: !data.effectQuality && !data.effectQualityRequestFailed }),
      reasonCode: data.effectQualityRequestFailed ? "learning_effect_quality_request_failed" : topCount(data.effectQuality?.reasonCounts ?? {}) ?? (effectStatus ?? "effect_quality_not_observed"),
      metric: data.effectQuality ? `${data.effectQuality.terminalCount} 终态 · ${Math.round((data.effectQuality.closureRatio ?? 0) * 100)}% 收口` : "效果待确认",
      details: [
        { label: "效果姿态", value: data.effectQuality?.status ?? "未返回" },
        { label: "观察中", value: String(data.effectQuality?.activeCount ?? "—") },
        { label: "重试候选", value: String(data.effectQuality?.retryCandidateCount ?? "—") },
      ],
      fact: data.applications.fact,
      icon: Gauge,
    },
    {
      id: "learning-return",
      sequence: 10,
      title: "回流实时循环",
      eyebrow: "10 / Return",
      role: "committed projection → next live loop",
      description: "闭环最后回到实时循环。投影只有在服务端提交后才会被下一轮 live loop 读取；页面不会把动画或建议误标为已发布。",
      source: "live.state.v2 · /ws/state",
      observedAt: fallbackLiveFact.observed_at,
      state: fallbackLiveFact.state,
      status: resolvedLearningStatus(fallbackLiveFact, { ready: liveActive, waiting: !liveActive }),
      reasonCode: fallbackLiveFact.reason_code ?? (!liveActive ? "live_loop_not_observed" : null),
      metric: liveActive ? "WS 完整快照已连接" : "实时循环待确认",
      details: [
        { label: "循环事实", value: fallbackLiveFact.state },
        { label: "下一轮", value: liveActive ? "继续消费服务端快照" : "等待完整快照" },
        { label: "执行权", value: "仅服务端拥有" },
      ],
      fact: fallbackLiveFact,
      icon: Radio,
    },
  ];
}

function architectureStatusLabel(status: ArchitectureNodeStatus): string {
  if (status === "active") return "当前活跃";
  if (status === "observed") return "已观测";
  if (status === "structural") return "架构节点";
  if (status === "blocked") return "已知阻塞";
  if (status === "waiting") return "等待输入";
  if (status === "stale") return "已过期";
  if (status === "error") return "读取错误";
  return "未确认";
}

function resolveArchitectureNode(draft: ArchitectureNodeDraft): ArchitectureNode {
  const { status: preferredStatus, statusLabel: preferredLabel, ...rest } = draft;
  const factState = draft.fact?.state;
  const status = preferredStatus ?? (factState === "error" ? "error" : factState === "stale" ? "stale" : factState === "known" ? "observed" : factState === "unknown" ? "unknown" : "structural");
  return {
    ...rest,
    input: draft.input ?? "未提供",
    output: draft.output ?? "未提供",
    observedAt: draft.observedAt ?? draft.fact?.observed_at ?? null,
    reasonCode: draft.reasonCode ?? draft.fact?.reason_code ?? null,
    status,
    statusLabel: preferredLabel ?? architectureStatusLabel(status),
  };
}

function architectureNodesWithFacts(...facts: Array<FactEnvelope | undefined>): FactEnvelope | undefined {
  return facts.find((fact) => fact?.state === "error") ?? facts.find((fact) => fact?.state === "known" || fact?.state === "stale") ?? facts.find(Boolean);
}

function buildArchitectureMap({
  snapshot,
  learningData,
  readinessFact,
  liveAlpha,
  liveActive,
}: {
  snapshot: LiveStateSnapshot | null;
  learningData: LearningLoopData;
  readinessFact: FactEnvelope;
  liveAlpha?: ReadinessDimension;
  liveActive: boolean;
}): ArchitectureMapData {
  const shadowFact = architectureNodesWithFacts(learningData.shadowQueue.fact, learningData.inferenceAudits.fact);
  const governanceFact = architectureNodesWithFacts(learningData.governanceCandidates.fact, learningData.governanceReviews.fact, learningData.governanceProposals.fact);
  const governanceError = [learningData.governanceCandidates.fact, learningData.governanceReviews.fact, learningData.governanceProposals.fact].find((fact) => fact.state === "error");
  const governanceReady = learningData.governanceReviews.items.length > 0 || learningData.governanceProposals.items.length > 0;
  const shadowReady = learningData.shadowQueue.count > 0 || learningData.inferenceAudits.count > 0;
  const effectObserved = learningData.effectQuality !== null;
  const lanes: ArchitectureLane[] = [
    {
      id: "execution",
      title: "实时执行与风险平面",
      eyebrow: "01 / canonical authority chain",
      note: "cTrader 是经纪商事实源；serial live loop、Safety、RiskPolicy 和执行对账共同组成唯一实时开仓链。",
      edgeLabels: ["spot / account / positions", "closed bar + frozen inputs", "signal → hard gate", "intent / protection", "reconcile", "state snapshot"],
      active: liveActive,
      nodes: [
        resolveArchitectureNode({ id: "arch-ctrader", title: "cTrader 权威源", eyebrow: "broker input", role: "spot · account · positions · execution", description: "经纪商返回的报价、账户、持仓和成交是交易事实的最上游来源；客户端和其他服务不能代替 broker 确认这些事实。", source: "cTrader / live.state.v2", observedAt: snapshot?.spot.fact.observed_at, reasonCode: snapshot?.spot.fact.reason_code, metric: snapshot ? "spot / account / positions" : "等待 broker 事实", input: "报价、账户、持仓、成交", output: "fresh broker snapshot", status: liveActive ? "active" : undefined, fact: snapshot?.spot.fact, icon: Radio }),
        resolveArchitectureNode({ id: "arch-live-loop", title: "串行实时循环", eyebrow: "serial owner", role: "closed-bar · reconcile · publish", description: "按既有 serial owner 顺序推进闭合 K 线、因子信号、账户和仓位对账，再发布完整 live.state.v2。", source: snapshot?.loop.fact.source ?? "live.loop.v2", observedAt: snapshot?.loop.fact.observed_at, reasonCode: snapshot?.loop.fact.reason_code, metric: snapshot?.loop.running === true ? "running · accepting=" + String(snapshot.loop.acceptingNewRisk) : "循环状态待确认", input: "broker snapshot + closed bars", output: "live.state.v2", status: liveActive ? "active" : undefined, fact: snapshot?.loop.fact, icon: Activity }),
        resolveArchitectureNode({ id: "arch-alpha", title: "因子与信号", eyebrow: "server compute", role: "closed-bar factors · alpha", description: "服务端使用闭合 K 线、外部 PIT 数据和运行配置计算因子与信号；前端只读取 readiness 投影。", source: "ops.backend-readiness.v2", observedAt: liveAlpha?.observedAt ?? readinessFact.observed_at, reasonCode: liveAlpha?.reasonCode ?? readinessFact.reason_code, metric: liveAlpha?.ready === true ? "live alpha ready" : liveAlpha?.ready === false ? "live alpha blocked" : "live alpha 未确认", input: "bars + external PIT + runtime config", output: "signal / factor contribution", status: liveAlpha?.ready === false ? "blocked" : liveAlpha?.ready === true ? "observed" : undefined, fact: readinessFact, icon: Sparkles }),
        resolveArchitectureNode({ id: "arch-safety-risk", title: "Safety / RiskPolicy", eyebrow: "hard gate", role: "safety plane · risk sizing", description: "Safety 处理必须立即禁止新增风险的硬事实；RiskPolicy 负责风险计算和最终仓位。两者不由前端重算。", source: snapshot?.safety.source ?? "live.safety-freshness.v1", observedAt: snapshot?.safety.observed_at, reasonCode: snapshot?.safety.reason_code ?? snapshot?.safetyBlockers[0] ?? null, metric: snapshot?.safetyBlockers.length ? `${snapshot.safetyBlockers.length} 个硬门 blocker` : "硬门已确认", input: "spot / account / positions / risk inputs", output: "allow / block + candidate volume", status: snapshot?.safetyBlockers.length ? "blocked" : liveActive ? "active" : undefined, fact: snapshot?.safety, icon: ShieldCheck }),
        resolveArchitectureNode({ id: "arch-execution", title: "执行与持仓对账", eyebrow: "broker mutation", role: "intent · protection · reconcile", description: "执行意图、保护状态、成交回执和持仓回对由 broker 与 live service 权威维护；unknown outcome 必须保持 fail-closed。", source: snapshot?.positions.fact.source ?? "live.positions.v2", observedAt: snapshot?.positions.fact.observed_at, reasonCode: snapshot?.positions.fact.reason_code, metric: snapshot?.positions.positions?.length ? `${snapshot.positions.positions.length} 个持仓事实` : "当前无已确认持仓", input: "validated action intent", output: "deal / protection / reconcile", status: liveActive ? "active" : undefined, fact: snapshot?.positions.fact, icon: Server }),
        resolveArchitectureNode({ id: "arch-state", title: "PostgreSQL state_v1", eyebrow: "durable authority", role: "runtime state · learning audit", description: "运行态、学习审计、治理提交和应用效果落在 PostgreSQL state_v1；这是持久事实和审计的权威存储。", source: "PostgreSQL · state_v1", observedAt: snapshot?.fact.observed_at, reasonCode: snapshot?.fact.reason_code, metric: snapshot ? "runtime snapshot 已发布" : "等待持久快照", input: "live loop + execution + learning audit", output: "canonical durable state", status: liveActive ? "active" : undefined, fact: snapshot?.fact, icon: Database }),
      ],
    },
    {
      id: "data",
      title: "市场与外部数据平面",
      eyebrow: "02 / time-aware inputs",
      note: "这些是计算和研究消费的数据源；release_at、fetched_at、source 和 K 线月库边界由后端负责，前端只标出路径。",
      edgeLabels: ["monthly bars", "release_at / fetched_at", "event windows", "frozen context"],
      active: false,
      nodes: [
        resolveArchitectureNode({ id: "arch-bars", title: "K 线月库", eyebrow: "market data", role: "bars_monthly / closed bars", description: "K 线按月保存，当前月份库是 live bars 的 durable replica；实时趋势 bar 仍由 cTrader 在线 feed 作为 live authority。", source: "data/bars_monthly/bars_YYYY_MM.duckdb", metric: "月库 + 当前月兼容链接", input: "cTrader trendbar / bars API", output: "closed-bar context", icon: Database }),
        resolveArchitectureNode({ id: "arch-external", title: "外部 PIT 数据", eyebrow: "research data", role: "COT · ETF · macro · external", description: "外部研究数据保存 release_at、fetched_at 和 source，因子与回测只能使用 release_at 之后可见的数据。", source: "data/external_data.duckdb", metric: "release_at 约束", input: "COT / ETF / macro sources", output: "PIT feature context", icon: Layers3 }),
        resolveArchitectureNode({ id: "arch-events", title: "经济事件库", eyebrow: "risk context", role: "event calendar · event sizing", description: "经济事件单独保存，事件窗口缩放由后端 event_sizing 读取；它不是前端的风险判断器。", source: "data/events.duckdb", metric: "event sizing 输入", input: "event calendar", output: "window multiplier / context", icon: Clock3 }),
        resolveArchitectureNode({ id: "arch-features", title: "冻结计算输入", eyebrow: "shared context", role: "factor · replay · learning", description: "因子、回放和学习使用带版本、时间和来源的冻结输入，避免研究结论反向改写 live 事实。", source: "backend / research / strategy", metric: "live + replay 共用纯计算", input: "bars + external + events + config", output: "factor / replay / evidence", icon: GitBranch }),
      ],
    },
    {
      id: "learning",
      title: "智能学习与模型平面",
      eyebrow: "03 / evidence → shadow",
      note: "学习 worker 负责物化、资格和观察性模型；shadow/advisory 永远不直接拥有实盘执行权。",
      edgeLabels: ["decision / close review", "sample materialize", "evidence contract", "model_ready gate", "shadow audit"],
      active: learningData.samples.fact.state === "known",
      nodes: [
        resolveArchitectureNode({ id: "arch-outcomes", title: "决策与结果事实", eyebrow: "learning input", role: "decision · supervisor · close review", description: "实时决策、持仓监督轨迹和成熟平仓复盘组成学习闭环的输入，不能用缺失结果推断模型效果。", source: "state_v1 · /api/learning/summary", observedAt: learningData.reviews.fact.observed_at, metric: `${learningData.reviews.count} 条复盘入口`, input: "decision ledger + close outcome", output: "learning anchors", fact: learningData.reviews.fact, icon: ListChecks }),
        resolveArchitectureNode({ id: "arch-samples", title: "学习样本物化", eyebrow: "learning worker", role: "autonomous_learning_sample", description: "学习 worker 将输入物化为统一样本，保留 label、integrity、causal level、train weight 和治理资格。", source: "state_v1 · /api/learning/autonomous/samples", observedAt: learningData.samples.fact.observed_at, metric: `${learningData.samples.count} 条样本`, input: "decision / trace / review anchors", output: "sample + evidence fields", fact: learningData.samples.fact, icon: Layers3 }),
        resolveArchitectureNode({ id: "arch-evidence", title: "证据合同与质量", eyebrow: "qualification", role: "integrity · causal · label · blockers", description: "统一 evidence_contract evaluator 检查完整性、因果等级、成熟标签、hash 和污染，不满足条件的样本保持隔离。", source: "state_v1 · /api/learning/dataset/quality-health", observedAt: learningData.quality.fact.observed_at, metric: `${learningData.quality.evidenceCounts.checked ?? 0} 条已检查`, input: "sample evidence contract", output: "allowed uses / blockers", fact: learningData.quality.fact, icon: ShieldCheck }),
        resolveArchitectureNode({ id: "arch-dataset", title: "数据集准入", eyebrow: "training gate", role: "trade + decision readiness", description: "后端按 model_ready 数量、schema issue 和证据门槛判断 ready、warming_up 或 not_ready。", source: "state_v1 · /api/learning/dataset/readiness", observedAt: learningData.dataset.fact.observed_at, metric: learningData.dataset.level ?? "准入待确认", input: "qualified samples", output: "training dataset / blockers", status: learningData.dataset.fact.state === "known" && learningData.dataset.ready === false ? "blocked" : undefined, fact: learningData.dataset.fact, icon: Database }),
        resolveArchitectureNode({ id: "arch-shadow", title: "Shadow / Advisory 模型", eyebrow: "observation only", role: "candidate queue · inference audit", description: "模型候选和推理审计只用于观察，不直接执行订单；没有 shadow model 时保持等待输入。", source: "model_registry · shadow queue + inference", observedAt: latestObservedAt(learningData.shadowQueue.fact, learningData.inferenceAudits.fact), reasonCode: shadowFact?.reason_code ?? (!shadowReady ? "no_shadow_model_observed" : null), metric: `${learningData.shadowQueue.count} 候选 · ${learningData.inferenceAudits.count} 条审计`, input: "training dataset + model artifact", output: "shadow prediction + audit", status: shadowFact?.state === "error" ? "error" : shadowReady ? "observed" : "waiting", fact: shadowFact, icon: Sparkles }),
      ],
    },
    {
      id: "governance",
      title: "治理、应用与后验平面",
      eyebrow: "04 / advisory → committed projection",
      note: "建议必须经过 review、V16、RiskPolicy 和 Coordinator；只有 committed projection 才能在下一轮被消费。",
      edgeLabels: ["policy suggestion", "review / proposal", "typed mutation", "effect ledger", "committed projection"],
      active: governanceReady || learningData.applications.fact.state === "known",
      nodes: [
        resolveArchitectureNode({ id: "arch-suggestions", title: "建议与因子证据", eyebrow: "advisory output", role: "policy_suggestion · factor catalog", description: "模型和复盘结果形成建议与因子证据；建议是治理输入，不等于批准、应用或运行时变更。", source: "state_v1 · /api/learning/suggestions", observedAt: learningData.suggestions.fact.observed_at, metric: `${learningData.suggestions.count} 条建议`, input: "shadow audit + matured evidence", output: "policy suggestion + factor evidence", fact: learningData.suggestions.fact, icon: WorkflowIcon }),
        resolveArchitectureNode({ id: "arch-coordinator", title: "Review / Coordinator", eyebrow: "governance gate", role: "candidate · review · proposal", description: "候选、审查和提案在治理链中经过既有 V16CommandGate、RiskPolicy 和 Coordinator 事务；客户端不拥有 approve/apply 权。", source: "state_v1 · governance candidate/review/proposal", observedAt: latestObservedAt(learningData.governanceCandidates.fact, learningData.governanceReviews.fact, learningData.governanceProposals.fact), reasonCode: governanceError?.reason_code ?? (!governanceReady ? "governance_review_not_observed" : null), metric: `${learningData.governanceCandidates.items.length} 候选 · ${learningData.governanceReviews.items.length} 审查`, input: "suggestion + evidence + review", output: "typed mutation intent", status: governanceError ? "error" : governanceReady ? "observed" : "waiting", fact: governanceFact, icon: GitBranch }),
        resolveArchitectureNode({ id: "arch-application", title: "受控应用账本", eyebrow: "typed mutation", role: "learning_application_log · runtime overlay", description: "通过既有治理事务提交的应用才进入 application log；提交前后的状态、mutation ID 和 audit ID 由服务端保存。", source: "state_v1 · /api/learning/applications", observedAt: learningData.applications.fact.observed_at, metric: `${learningData.applications.count} 条应用`, input: "committed mutation + domain hook", output: "runtime overlay + audit row", fact: learningData.applications.fact, icon: CheckCircle2 }),
        resolveArchitectureNode({ id: "arch-effect", title: "后验效果与回滚观察", eyebrow: "posterior", role: "effect ledger · rollback observation", description: "应用后的效果、终态收口和 rollback observation 是治理后验；效果不足时不能自动解释为应该继续扩权。", source: "state_v1 · /api/learning/effect-quality", observedAt: learningData.applications.fact.observed_at, reasonCode: learningData.effectQualityRequestFailed ? "learning_effect_quality_request_failed" : learningData.effectQuality?.status === "degraded" ? "effect_quality_degraded" : null, metric: learningData.effectQuality ? `${learningData.effectQuality.terminalCount} 终态 · ${Math.round((learningData.effectQuality.closureRatio ?? 0) * 100)}% 收口` : "效果待确认", input: "application + observation window", output: "effect quality + rollback signal", status: learningData.effectQuality?.status === "degraded" ? "blocked" : effectObserved ? "observed" : "waiting", fact: learningData.applications.fact, icon: Gauge }),
        resolveArchitectureNode({ id: "arch-commit-return", title: "Committed runtime projection", eyebrow: "return to live", role: "config snapshot → next loop", description: "只有 committed mutation 和 runtime projection 才会回到下一轮实时循环；建议、动画和旧历史记录不会直接改变 live。", source: "runtime_config_overlay + committed snapshot", observedAt: learningData.applications.fact.observed_at, metric: "下一轮 live loop 消费", input: "committed runtime snapshot", output: "next-loop configuration projection", status: liveActive ? "active" : "waiting", fact: learningData.applications.fact, icon: ArrowUp }),
      ],
    },
    {
      id: "ops",
      title: "服务、就绪与运维观测平面",
      eyebrow: "05 / health / recovery / logs",
      note: "运维层读取 backend、worker、readiness、recovery 和日志事实；它可以定位问题，但不重新计算交易或学习授权。",
      edgeLabels: ["process health", "readiness fact", "diagnostic stream"],
      active: readinessFact.state === "known" || liveActive,
      nodes: [
        resolveArchitectureNode({ id: "arch-services", title: "Backend / Workers", eyebrow: "process runtime", role: "backend · learning worker · job worker", description: "后端和 worker 负责真实服务、学习重任务和持久任务边界；客户端只读取它们发布的 capability/readiness 事实。", source: "systemd · backend / learning worker", observedAt: readinessFact.observed_at, reasonCode: readinessFact.reason_code, metric: readinessFact.state === "known" ? "服务端事实已发布" : "服务进程待确认", input: "runtime config + durable state", output: "health / capability / readiness", fact: readinessFact, icon: Server }),
        resolveArchitectureNode({ id: "arch-readiness", title: "Readiness 投影", eyebrow: "read-only gate view", role: "execution · alpha · mutation · release", description: "Readiness 只读检查 canonical 事实是否存在、新鲜、可用，并发布 blocker；它不重新计算风险，也不拥有开关提交权。", source: "ops.backend-readiness.v2", observedAt: readinessFact.observed_at, reasonCode: readinessFact.reason_code, metric: `${readinessFact.state} · ${readinessFact.state === "known" ? "四维就绪投影" : "就绪待确认"}`, input: "canonical runtime facts", output: "dimension status + reason code", fact: readinessFact, icon: ShieldCheck }),
        resolveArchitectureNode({ id: "arch-observability", title: "Health / Logs / Recovery", eyebrow: "diagnostic stream", role: "health · logs · recovery · alerts", description: "健康探针、滚动日志和 recovery 记录用于定位服务问题；它们是诊断入口，不是第二套交易或治理 authority。", source: "/api/health · /api/logs/tail · /api/ops/recovery", metric: "diagnostic-only path", input: "process events + durable errors", output: "log tail + recovery evidence", icon: Activity }),
      ],
    },
    {
      id: "client",
      title: "API 与客户端消费平面",
      eyebrow: "06 / read-only projection",
      note: "API/WSS 和桌面/小程序只消费服务端事实；客户端展示路径不拥有 broker、数据库、风险或治理写入权。",
      edgeLabels: ["live.state.v2 / fact.v1", "read-only workspace", "status surface"],
      active: liveActive,
      nodes: [
        resolveArchitectureNode({ id: "arch-api", title: "API / WSS 事实投影", eyebrow: "server read model", role: "fact.v1 · /api/* · /ws/state", description: "后端把 canonical state、readiness、学习和治理结果序列化为 endpoint-specific fact.v1；/ws/state 是唯一实时状态来源。", source: "FastAPI API + /ws/state", observedAt: snapshot?.fact.observed_at, metric: liveActive ? "WSS connected · fact.v1" : "等待 WSS 首帧", input: "state_v1 + domain read models", output: "fact.v1 / live.state.v2", status: liveActive ? "active" : undefined, fact: snapshot?.fact, icon: Radio }),
        resolveArchitectureNode({ id: "arch-tauri", title: "Tauri / React Workbench", eyebrow: "desktop client", role: "six workspaces · local shell", description: "桌面端消费 API/WSS，展示事实、收集操作意图并执行本地 UI 偏好；不连接 broker、不写 PostgreSQL。", source: "web_frontend · Tauri 2", observedAt: liveActive ? snapshot?.fact.observed_at : null, metric: liveActive ? "本地操作台已连接" : "本地客户端待连接", input: "fact.v1 + live.state.v2", output: "read-only view + action intent", status: liveActive ? "active" : "waiting", fact: snapshot?.fact, icon: Gauge }),
        resolveArchitectureNode({ id: "arch-miniprogram", title: "小程序状态面", eyebrow: "lightweight client", role: "简洁状态 / PnL surface", description: "小程序只承接简洁状态和收益图等轻量视图；复杂图表、研究、治理和运维由本地桌面端承接。", source: "miniprogram_v2", metric: "status-only surface", input: "lightweight API projection", output: "status / PnL display", icon: Radio }),
      ],
    },
  ];
  return { lanes, nodes: lanes.flatMap((lane) => lane.nodes) };
}

function architectureStatusClass(status: ArchitectureNodeStatus): string {
  return `architecture-status-${status}`;
}

function ArchitectureNodeCard({ node, selected, onSelect }: { node: ArchitectureNode; selected: boolean; onSelect: (id: string) => void }) {
  const Icon = node.icon;
  return <button type="button" className={`architecture-node ${architectureStatusClass(node.status)} ${selected ? "architecture-node-selected" : ""}`} aria-pressed={selected} aria-label={`${node.title}，${node.statusLabel}，${node.metric}`} onClick={() => onSelect(node.id)}>
    <span className="architecture-node-head"><span>{node.eyebrow}</span><Icon size={14} aria-hidden="true" /></span>
    <strong>{node.title}</strong>
    <span className="architecture-node-role">{node.role}</span>
    <span className="architecture-node-metric">{node.metric}</span>
    <span className={`architecture-node-status ${architectureStatusClass(node.status)}`}><i aria-hidden="true" />{node.statusLabel}</span>
    <time>{formatObservedTime(node.observedAt, "架构定义")}</time>
  </button>;
}

function ArchitectureEdge({ label }: { label: string }) {
  return <span className="architecture-edge" aria-hidden="true"><span>{label}</span><i /></span>;
}

function ArchitectureNetwork({ data, selectedId, onSelect, learningStages, bottleneck }: { data: ArchitectureMapData; selectedId: string; onSelect: (id: string) => void; learningStages: LearningStage[]; bottleneck: LearningStage }) {
  const learningReadyCount = learningStages.filter((stage) => stage.status === "ready").length;
  const learningPendingCount = learningStages.filter((stage) => stage.status !== "ready").length;
  const liveNode = data.nodes.find((node) => node.id === "arch-live-loop");
  const returnNode = data.nodes.find((node) => node.id === "arch-commit-return");
  return <Panel title="项目架构拓扑 · 一体化神经网络" eyebrow="runtime spine / learning feedback / transport projection" className="architecture-panel">
    <div className="architecture-integrated-summary">
      <div className={`architecture-summary-bottleneck ${architectureStatusClass(bottleneck.status === "error" ? "error" : bottleneck.status === "blocked" ? "blocked" : bottleneck.status === "stale" ? "stale" : bottleneck.status === "waiting" ? "waiting" : "observed")}`}><span><AlertTriangle size={14} aria-hidden="true" />当前学习卡点</span><strong>{bottleneck.title}</strong><small>{bottleneck.reasonCode ?? "该阶段已形成可确认输出"} · {learningPendingCount} 个阶段待推进</small></div>
      <div className="architecture-summary-metric"><span>实时主干</span><strong>{liveNode?.metric ?? "实时链路待确认"}</strong><small>{liveNode?.statusLabel ?? "未确认"} · cTrader → live loop → state_v1</small></div>
      <div className="architecture-summary-metric"><span>学习闭环</span><strong>{learningReadyCount} / {learningStages.length} 阶段</strong><small>复盘 → 样本 → 证据 → Shadow → 治理 → 回流</small></div>
      <div className="architecture-summary-metric"><span>回流出口</span><strong>{returnNode?.metric ?? "待确认"}</strong><small>只有 committed projection 才回到实时主干</small></div>
    </div>
    <div className="architecture-network-legend"><span><i className="architecture-legend-dot architecture-status-active" />当前链路</span><span><i className="architecture-legend-dot architecture-status-observed" />已观测</span><span><i className="architecture-legend-dot architecture-status-structural" />架构节点</span><span><i className="architecture-legend-dot architecture-status-blocked" />阻塞 / 错误</span><span className="architecture-legend-note"><Radio size={12} />粒子沿传输方向移动；点击节点看输入、输出和事实来源</span></div>
    <div className="architecture-network" role="list" aria-label="项目架构节点与传输路径">
      {data.lanes.map((lane) => <section className={`architecture-lane architecture-lane-${lane.id} ${lane.active ? "architecture-lane-active" : ""}`} key={lane.id} role="listitem">
        <div className="architecture-lane-heading"><div><strong>{lane.title}</strong><span>{lane.eyebrow}</span></div><small>{lane.note}</small></div>
        <div className="architecture-lane-flow">
          {lane.nodes.map((node, index) => <div className="architecture-flow-item" key={node.id}>
            <ArchitectureNodeCard node={node} selected={selectedId === node.id} onSelect={onSelect} />
            {index < lane.nodes.length - 1 ? <ArchitectureEdge label={lane.edgeLabels[index] ?? "transport"} /> : null}
          </div>)}
        </div>
      </section>)}
    </div>
    <div className="architecture-cross-links" aria-label="跨平面传输关系"><span><ArrowRight size={13} />市场 / 外部数据 → 因子与信号</span><span><ArrowRight size={13} />实时决策 / 复盘 → 学习样本</span><span><ArrowRight size={13} />Committed projection → 下一轮 live loop</span><span><ArrowRight size={13} />服务健康 → readiness / logs</span><span><ArrowRight size={13} />API / WSS → 桌面与小程序</span></div>
    <p className="architecture-network-note"><AlertTriangle size={13} aria-hidden="true" />这是一张统一总拓扑：实时路径是主干，智能学习是从 state_v1 分出的反馈回路，治理提交通过 committed projection 回到下一轮 live loop；服务、数据和客户端都是同一张图里的输入、处理或消费节点。已观测节点携带真实 <code>fact.v1</code> 和 <code>observed_at</code>；动效只表达数据传输方向，不表示产生订单、批准治理或自动改参。</p>
  </Panel>;
}

function ArchitectureNodeDetail({ node }: { node: ArchitectureNode }) {
  const Icon = node.icon;
  return <DialogSurface className="architecture-detail-dialog">
    <div className="architecture-detail-dialog-title-row"><div><span className="wb-eyebrow">点击拓扑节点查看职责与传输</span><DialogTitle className="architecture-detail-dialog-title">{node.title}</DialogTitle></div><DialogClose asChild><button type="button" className="architecture-detail-close" aria-label="关闭架构节点详情"><X size={16} /></button></DialogClose></div>
    <div className="architecture-detail-head"><span className={`architecture-detail-icon ${architectureStatusClass(node.status)}`}><Icon size={18} /></span><div><strong>{node.eyebrow}</strong><span>{node.role}</span></div><span className={`architecture-node-status ${architectureStatusClass(node.status)}`}><i aria-hidden="true" />{node.statusLabel}</span></div>
    <p className="architecture-detail-description">{node.description}</p>
    <div className="architecture-detail-facts"><div><span>输入</span><strong>{node.input}</strong></div><div><span>输出</span><strong>{node.output}</strong></div><div><span>当前摘要</span><strong>{node.metric}</strong></div></div>
    <dl className="workflow-detail-meta"><div><dt>事实来源</dt><dd>{node.source}</dd></div><div><dt>最近观测</dt><dd>{formatObservedTime(node.observedAt, "架构定义 / 未提供运行观测")}</dd></div><div><dt>原因码</dt><dd>{node.reasonCode ?? "无"}</dd></div></dl>
    <div className="workflow-detail-note"><AlertTriangle size={14} aria-hidden="true" /><span>节点详情只说明职责和传输边界。客户端不在这里计算风险、readiness、学习资格或治理授权。</span></div>
    {node.fact ? <FactBadge fact={node.fact} label="节点事实" /> : <span className="architecture-structural-note"><GitBranch size={13} />代码 / 数据边界，当前没有独立运行 Fact</span>}
  </DialogSurface>;
}

export function WorkflowPage() {
  const live = useLiveState();
  const readiness = useQuery({ queryKey: ["workbench", "readiness"], queryFn: getReadinessView, staleTime: 15_000, retry: false });
  const learning = useQuery({ queryKey: ["workflow", "learning-loop"], queryFn: getLearningLoopData, staleTime: 30_000, refetchInterval: 30_000, retry: false });
  const snapshot = live.snapshot;
  const readinessFact = readiness.data?.fact ?? unknownFact("ops.backend-readiness.v2", readiness.error ? "readiness_request_failed" : "readiness_not_loaded", readiness.error ? "error" : "unknown");
  const readinessDimensions = readiness.data?.dimensions;
  const liveAlpha = dimensionFor(readinessDimensions, "live alpha");
  const learningData = learning.data ?? emptyLearningLoopData();
  const liveActive = live.connection === "connected" && Boolean(snapshot);
  const [selectedArchitectureId, setSelectedArchitectureId] = useState<string | null>(null);
  const learningStages = useMemo(() => buildLearningStages(learningData, liveActive, snapshot?.loop.fact), [learningData, liveActive, snapshot?.loop.fact]);
  const bottleneck = learningStages.find((stage) => stage.status !== "ready") ?? learningStages[learningStages.length - 1];
  const architecture = useMemo(() => buildArchitectureMap({ snapshot, learningData, readinessFact, liveAlpha, liveActive }), [snapshot, learningData, readinessFact, liveAlpha, liveActive]);
  const selectedArchitecture = architecture.nodes.find((node) => node.id === selectedArchitectureId);
  const connectedLabel = liveActive ? "当前链路活跃" : live.connection === "connecting" ? "等待完整快照" : "链路未确认";

  return <div className="workspace-page workflow-page">
    <WorkspaceTitle kicker="06 / 运行路径" title="项目架构与工作流" description="把实时执行、数据、智能学习、治理和客户端之间的真实传输路径展开，点击任一节点查看它当前负责什么、输入什么、输出什么。" />
    <div className="workspace-toolbar workflow-toolbar"><span><GitBranch size={14} />架构拓扑 / 实时执行 / 智能学习 / 客户端</span><span><Radio size={14} />{connectedLabel}</span><FactBadge compact fact={readinessFact} label="就绪投影" /><span><Clock3 size={14} />完整快照 / {formatObservedTime(live.lastCompleteSnapshotAt, "待确认")}</span></div>
    <Dialog open={selectedArchitecture !== undefined} onOpenChange={(open) => { if (!open) setSelectedArchitectureId(null); }}>
      <ArchitectureNetwork data={architecture} selectedId={selectedArchitectureId ?? ""} onSelect={setSelectedArchitectureId} learningStages={learningStages} bottleneck={bottleneck} />
      {selectedArchitecture ? <ArchitectureNodeDetail node={selectedArchitecture} /> : null}
    </Dialog>
  </div>;
}
