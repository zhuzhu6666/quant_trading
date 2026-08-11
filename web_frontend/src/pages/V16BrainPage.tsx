import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BrainCircuit,
  CircleDot,
  FileSearch2,
  GitBranch,
  ListChecks,
  Network,
  Play,
  RefreshCw,
  Route,
  ShieldCheck,
  SlidersHorizontal,
  UsersRound,
} from "lucide-react";
import {
  evaluateLiveAutonomyUnlock,
  evaluateBrainLiveReadyGuardrail,
  getAgentAuthority,
  getAgentBriefing,
  getAgentChainHealth,
  getAgentScorecard,
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
  isStepUpRequiredError,
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
import { ActionButton } from "@/components/ActionButton";
import { CompactMetric, Field, SectionHead, toneFromStatus, type Tone } from "@/components/DashboardBits";
import { FactBoundary } from "@/components/FactBoundary";
import { JsonBlock } from "@/components/JsonBlock";
import { StatusPill } from "@/components/StatusPill";
import {
  decodeLiveAutonomyEvaluation,
  decodeLiveAutonomyStatus,
  factBoundTone,
  factIsKnown,
  readFact,
} from "@/api/fact";
import { formatDecimal } from "@/lib/format";
import { useBackendReadinessQuery } from "@/hooks/useCoreQueries";
import {
  ActionPlanList,
  AgentAuthorityPanel,
  CoveragePanel,
  EvaluationList,
  ExecutionList,
  GovernancePipeline,
  GuardrailList,
  HypothesisList,
  MemoryList,
  RuntimeLog,
  asRecord,
  boolTone,
  countOf,
  displayContract,
  displayAction,
  displayStage,
  displayValue,
  formatTime,
  pick,
  pickArray,
  pickBoolean,
  pickNumber,
  pickString,
  scorePct,
  statusTone,
} from "@/features/v16/V16BrainViews";

type ChainTab = "overview" | "proposals" | "evidence" | "control";

const chainTabs: Array<{ key: ChainTab; label: string; icon: typeof Network }> = [
  { key: "overview", label: "现在怎么运行", icon: Network },
  { key: "proposals", label: "卡在哪 / 待治理", icon: GitBranch },
  { key: "evidence", label: "依据与反馈", icon: FileSearch2 },
  { key: "control", label: "执行边界", icon: SlidersHorizontal },
];

function queryTone(known: boolean, failed: boolean, tone: Tone): Tone {
  if (failed) return "bad";
  return known ? tone : "pending";
}

function queryStatus(known: boolean, failed: boolean, value: string, fallback = "待确认"): string {
  if (failed) return "读取错误";
  return known ? value || "未形成" : fallback;
}

export function V16BrainPage({ embedded = false }: { embedded?: boolean }) {
  const [activeTab, setActiveTab] = useState<ChainTab>("overview");
  const queryClient = useQueryClient();
  const readinessQuery = useBackendReadinessQuery();
  const agentAuthorityQuery = useQuery({ queryKey: ["v16", "agent-authority"], queryFn: getAgentAuthority, enabled: activeTab === "overview", refetchInterval: 30_000, staleTime: 10_000 });
  const agentScorecardQuery = useQuery({ queryKey: ["v16", "agent-scorecard"], queryFn: () => getAgentScorecard(300), enabled: activeTab === "overview", refetchInterval: 30_000, staleTime: 10_000 });
  const agentBriefingQuery = useQuery({ queryKey: ["v16", "agent-briefing"], queryFn: () => getAgentBriefing(20), enabled: activeTab === "overview" || activeTab === "proposals", refetchInterval: 30_000, staleTime: 10_000 });
  const agentChainHealthQuery = useQuery({ queryKey: ["v16", "agent-chain-health"], queryFn: () => getAgentChainHealth(300), enabled: activeTab === "overview", refetchInterval: 30_000, staleTime: 10_000 });
  const brainStateQuery = useQuery({ queryKey: ["v16", "brain-state"], queryFn: () => getBrainState(false), enabled: activeTab === "overview" || activeTab === "evidence", refetchInterval: 20_000, staleTime: 8_000 });
  const brainMemoryQuery = useQuery({ queryKey: ["v16", "brain-memory"], queryFn: () => getBrainMemory(false, 24), enabled: activeTab === "overview" || activeTab === "evidence", refetchInterval: 30_000, staleTime: 10_000 });
  const brainActionPlansQuery = useQuery({ queryKey: ["v16", "brain-action-plans"], queryFn: () => getBrainActionPlans(false, 24), enabled: activeTab === "evidence", refetchInterval: 30_000, staleTime: 10_000 });
  const brainActionPlanEvalsQuery = useQuery({ queryKey: ["v16", "brain-action-plan-evals"], queryFn: () => getBrainActionPlanEvals(false, 24), enabled: activeTab === "evidence", refetchInterval: 30_000, staleTime: 10_000 });
  const lowImpactExecutionsQuery = useQuery({ queryKey: ["v16", "brain-low-impact-executions"], queryFn: () => getBrainLowImpactExecutions(24), enabled: activeTab === "evidence", refetchInterval: 30_000, staleTime: 10_000 });
  const mediumImpactGovernanceQuery = useQuery({ queryKey: ["v16", "brain-medium-impact-governance"], queryFn: () => getBrainMediumImpactGovernance(24), enabled: activeTab === "proposals", refetchInterval: 30_000, staleTime: 10_000 });
  const candidateReviewsQuery = useQuery({ queryKey: ["v16", "brain-governance-candidate-reviews"], queryFn: () => getBrainGovernanceCandidateReviews(24), enabled: activeTab === "proposals", refetchInterval: 30_000, staleTime: 10_000 });
  const liveReadyGuardrailsQuery = useQuery({ queryKey: ["v16", "brain-live-ready-guardrails"], queryFn: () => getBrainLiveReadyGuardrails(24), enabled: activeTab === "control", refetchInterval: 30_000, staleTime: 10_000 });
  const proposalRegistryQuery = useQuery({ queryKey: ["autonomy", "proposal-registry"], queryFn: () => getAutonomyProposals(false, 24), enabled: activeTab === "proposals", refetchInterval: 30_000, staleTime: 10_000 });
  const liveAutonomyQuery = useQuery({ queryKey: ["autonomy", "live-status"], queryFn: () => getLiveAutonomyStatus(false), enabled: activeTab === "overview" || activeTab === "control", refetchInterval: 20_000, staleTime: 8_000 });

  const refreshMutation = useMutation({
    mutationFn: async () => {
      if (activeTab === "overview") {
        await Promise.all([
          getBrainState(true),
          getBrainMemory(true, 24),
          getAgentAuthority(),
          getAgentScorecard(300),
          getAgentBriefing(20),
          getAgentChainHealth(300),
        ]);
      } else if (activeTab === "proposals") {
        await Promise.all([
          getBrainMediumImpactGovernance(24),
          getBrainGovernanceCandidateReviews(24),
          getAutonomyProposals(true, 24),
          getAgentBriefing(20),
        ]);
      } else if (activeTab === "evidence") {
        await getBrainState(true);
        await getBrainMemory(true, 24);
        await getBrainActionPlans(true, 24);
        await getBrainActionPlanEvals(true, 24);
        await getBrainLowImpactExecutions(24);
      } else {
        await getBrainLiveReadyGuardrails(24);
        await getLiveAutonomyStatus(true);
      }
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
    mutationFn: () => unlockLiveAutonomy(),
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

  const runLiveUnlock = async () => {
    try {
      return await liveUnlockMutation.mutateAsync();
    } catch (error) {
      if (isStepUpRequiredError(error)) liveUnlockMutation.reset();
      throw error;
    }
  };

  const readiness = asRecord(readinessQuery.data);
  const readinessFact = readFact(readinessQuery.data, "ops.backend-readiness.v2");
  const readinessRequestFailed = readinessQuery.isError || readinessQuery.isRefetchError;
  const readinessKnown = factIsKnown(readinessFact, readinessRequestFailed);
  const v16Readiness = asRecord(pick(readiness, ["v16"]));
  const brainState = asRecord(pick(brainStateQuery.data, ["brain_state"]));
  const brainStateFact = readFact(brainStateQuery.data, "ops.v16-brain-state.v2");
  const brainStateRequestFailed = brainStateQuery.isError || brainStateQuery.isRefetchError;
  const brainStateKnown = factIsKnown(brainStateFact, brainStateRequestFailed);
  const worldModel = asRecord(pick(brainState, ["world_model"]));
  const memoryFromState = asRecord(pick(brainState, ["memory"]));
  const memoryFromQuery = asRecord(pick(brainMemoryQuery.data, ["memory"]));
  const hasMemoryFromState = Object.keys(memoryFromState).length > 0;
  const memory = hasMemoryFromState ? memoryFromState : memoryFromQuery;
  const memoryFact = hasMemoryFromState ? brainStateFact : readFact(brainMemoryQuery.data, "ops.v16-brain-memory.v2");
  const memoryRequestFailed = hasMemoryFromState
    ? brainStateRequestFailed
    : brainMemoryQuery.isError || brainMemoryQuery.isRefetchError;
  const memoryKnown = factIsKnown(memoryFact, memoryRequestFailed);
  const critic = asRecord(pick(brainState, ["critic"]));
  const hypotheses = pickArray(brainState, ["hypotheses"]);
  const memoryItems = pickArray(memory, ["items"]);
  const negativeMemory = pickArray(memory, ["negative_matches"]);
  const counterEvidence = pickArray(memory, ["counter_evidence"]);
  const evidenceRefs = asRecord(pick(brainState, ["evidence_refs"]));
  const boundary = asRecord(pick(brainState, ["boundary"]));
  const sourceGaps = pickArray(memory, ["source_gaps"]);
  const actionPlanRun = asRecord(pick(brainActionPlansQuery.data, ["action_plans"]));
  const actionPlanFact = readFact(brainActionPlansQuery.data, "ops.v16-action-plans.v2");
  const actionPlanRequestFailed = brainActionPlansQuery.isError || brainActionPlansQuery.isRefetchError;
  const actionPlanKnown = factIsKnown(actionPlanFact, actionPlanRequestFailed);
  const actionPlans = pickArray(actionPlanRun, ["plans"]);
  const actionPlanEvalRun = asRecord(pick(brainActionPlanEvalsQuery.data, ["action_plan_evals"]));
  const actionPlanEvalFact = readFact(brainActionPlanEvalsQuery.data, "ops.v16-action-plan-evals.v2");
  const actionPlanEvalRequestFailed = brainActionPlanEvalsQuery.isError || brainActionPlanEvalsQuery.isRefetchError;
  const actionPlanEvalKnown = factIsKnown(actionPlanEvalFact, actionPlanEvalRequestFailed);
  const actionPlanEvals = pickArray(actionPlanEvalRun, ["evals"]);
  const lowImpactExecutionRun = asRecord(pick(lowImpactExecutionsQuery.data, ["low_impact_executions"]));
  const lowImpactFact = readFact(lowImpactExecutionsQuery.data, "ops.v16-low-impact-executions.v2");
  const lowImpactRequestFailed = lowImpactExecutionsQuery.isError || lowImpactExecutionsQuery.isRefetchError;
  const lowImpactKnown = factIsKnown(lowImpactFact, lowImpactRequestFailed);
  const lowImpactExecutions = pickArray(lowImpactExecutionRun, ["executions"]);
  const mediumImpactGovernanceRun = asRecord(pick(mediumImpactGovernanceQuery.data, ["medium_impact_governance"]));
  const mediumImpactFact = readFact(mediumImpactGovernanceQuery.data, "ops.v16-medium-impact-governance.v2");
  const mediumImpactRequestFailed = mediumImpactGovernanceQuery.isError || mediumImpactGovernanceQuery.isRefetchError;
  const mediumImpactKnown = factIsKnown(mediumImpactFact, mediumImpactRequestFailed);
  const mediumImpactGovernance = pickArray(mediumImpactGovernanceRun, ["items"]);
  const candidateReviewRun = asRecord(pick(candidateReviewsQuery.data, ["candidate_reviews"]));
  const candidateReviewFact = readFact(candidateReviewsQuery.data, "ops.v16-governance-candidate-reviews.v2");
  const candidateReviewRequestFailed = candidateReviewsQuery.isError || candidateReviewsQuery.isRefetchError;
  const candidateReviewKnown = factIsKnown(candidateReviewFact, candidateReviewRequestFailed);
  const candidateReviews = pickArray(candidateReviewRun, ["items"]);
  const liveReadyGuardrailRun = asRecord(pick(liveReadyGuardrailsQuery.data, ["live_ready_guardrails"]));
  const liveReadyGuardrailFact = readFact(liveReadyGuardrailsQuery.data, "ops.v16-live-ready-guardrails.v2");
  const liveReadyGuardrailRequestFailed = liveReadyGuardrailsQuery.isError || liveReadyGuardrailsQuery.isRefetchError;
  const liveReadyGuardrailKnown = factIsKnown(liveReadyGuardrailFact, liveReadyGuardrailRequestFailed);
  const liveReadyGuardrails = pickArray(liveReadyGuardrailRun, ["items"]);
  const latestGuardrail = asRecord(liveReadyGuardrails[0]);
  const proposalRegistry = asRecord(pick(proposalRegistryQuery.data, ["proposals"]));
  const proposalRegistryFact = readFact(proposalRegistryQuery.data, "ops.autonomy-proposals.v2");
  const proposalRegistryRequestFailed = proposalRegistryQuery.isError || proposalRegistryQuery.isRefetchError;
  const proposalRegistryKnown = factIsKnown(proposalRegistryFact, proposalRegistryRequestFailed);
  const proposalItems = pickArray(proposalRegistry, ["items"]);
  const proposalSummary = asRecord(pick(proposalRegistry, ["summary"]));
  const liveAutonomyView = decodeLiveAutonomyStatus(
    liveAutonomyQuery.data,
    liveAutonomyQuery.isError || liveAutonomyQuery.isRefetchError,
  );
  const latestEvaluationView = liveUnlockEvaluateMutation.data
    ? decodeLiveAutonomyEvaluation(
        liveUnlockEvaluateMutation.data,
        liveUnlockEvaluateMutation.isError,
      )
    : null;
  const liveAutonomy = liveAutonomyView.liveAutonomy;
  const liveAutonomyRequestFailed = liveAutonomyQuery.isError || liveAutonomyQuery.isRefetchError;
  const liveAutonomyKnown = factIsKnown(liveAutonomyView.fact, liveAutonomyRequestFailed);
  const latestEvaluationDisplayable = latestEvaluationView?.fact.state === "known"
    || latestEvaluationView?.fact.state === "stale";
  const liveAutonomyEvaluation = latestEvaluationDisplayable
    ? latestEvaluationView?.evaluation ?? liveAutonomyView.evaluation
    : liveAutonomyView.evaluation;
  const unlockAllowed = liveAutonomyKnown
    && (latestEvaluationView?.unlockAllowed ?? liveAutonomyView.unlockAllowed);
  const liveAutonomyBlockers = pickArray(liveAutonomyEvaluation, ["blockers"]);
  const liveAutonomyPosture = asRecord(pick(liveAutonomy, ["operational_posture"]));
  const autonomousBlueprint = v16Readiness;
  const agentAuthority = asRecord(pick(agentAuthorityQuery.data, ["status"]));
  const agentAuthorityFact = readFact(agentAuthorityQuery.data, "ops.agent-authority.v2");
  const agentAuthorityRequestFailed = agentAuthorityQuery.isError || agentAuthorityQuery.isRefetchError;
  const agentAuthorityKnown = factIsKnown(agentAuthorityFact, agentAuthorityRequestFailed);
  const agentScorecard = asRecord(pick(agentScorecardQuery.data, ["scorecard"]));
  const agentScorecardFact = readFact(agentScorecardQuery.data, "ops.agent-scorecard.v2");
  const agentScorecardRequestFailed = agentScorecardQuery.isError || agentScorecardQuery.isRefetchError;
  const agentScorecardKnown = factIsKnown(agentScorecardFact, agentScorecardRequestFailed);
  const agentBriefing = asRecord(pick(agentBriefingQuery.data, ["briefing"]));
  const agentBriefingFact = readFact(agentBriefingQuery.data, "ops.agent-briefing.v2");
  const agentBriefingRequestFailed = agentBriefingQuery.isError || agentBriefingQuery.isRefetchError;
  const agentBriefingKnown = factIsKnown(agentBriefingFact, agentBriefingRequestFailed);
  const agentChainHealth = asRecord(
    pick(agentChainHealthQuery.data, ["agent_chain_health"]),
  );
  const agentChainHealthFact = readFact(agentChainHealthQuery.data, "ops.agent-chain-health.v2");
  const agentChainHealthRequestFailed = agentChainHealthQuery.isError || agentChainHealthQuery.isRefetchError;
  const agentChainHealthKnown = factIsKnown(agentChainHealthFact, agentChainHealthRequestFailed);
  const governanceCoverage = asRecord(
    pick(agentBriefing, ["governance_coverage"]),
  );
  const proposalContextCoverage = asRecord(
    pick(governanceCoverage, ["proposal_generation_context_coverage"]),
  );
  const candidateContextCoverage = asRecord(
    pick(governanceCoverage, ["candidate_generation_context_coverage"]),
  );
  const candidateBridgeReviewCoverage = asRecord(
    pick(governanceCoverage, ["candidate_bridge_review_coverage"]),
  );

  const statTone = useMemo<Tone>(() => {
    const posture = pickString(worldModel, ["strategy_posture"], "");
    if (["normal"].includes(posture)) return "ok";
    if (["no_new_risk", "observation_only"].includes(posture)) return "bad";
    return "warn";
  }, [worldModel]);
  const criticVerdict = pickString(critic, ["verdict"], pickString(brainState, ["critic_verdict"], ""));
  const readOnly = pickBoolean(brainState, ["read_only"], true) && pickBoolean(boundary, ["read_only"], true);
  const affectsTrading = pickBoolean(brainState, ["affects_trading"], false);
  const marketRegime = displayValue(pickString(worldModel, ["market_regime"], "未提供"));
  const strategyPosture = displayValue(pickString(worldModel, ["strategy_posture"], "未提供"));
  const executionPosture = displayValue(pickString(worldModel, ["execution_posture"], "未提供"));
  const agentFactsFailed = agentAuthorityRequestFailed || agentChainHealthRequestFailed;
  const agentFactsKnown = agentAuthorityKnown && agentChainHealthKnown;
  const agentStatus = pickString(agentChainHealth, ["status"], pickString(agentAuthority, ["status"], ""));
  const blueprintStatus = displayValue(pickString(autonomousBlueprint, ["status"], ""));
  const blueprintBlockerCount = pickNumber(autonomousBlueprint, ["blocker_count"], 0);
  const proposalMissing = pickNumber(proposalContextCoverage, ["missing_required_context_count"], 0);
  const candidateMissing = pickNumber(candidateContextCoverage, ["missing_required_context_count"], 0);
  const reviewMissing = pickNumber(candidateBridgeReviewCoverage, ["missing_required_review_count"], 0);
  const latestActionPlan = asRecord(actionPlans[0]);
  const latestActionPlanEval = asRecord(actionPlanEvals[0]);
  const latestLowImpactExecution = asRecord(lowImpactExecutions[0]);

  const runtimeRows = [
    {
      icon: Network,
      label: "运行事实",
      status: queryStatus(brainStateKnown, brainStateRequestFailed, "已读取"),
      tone: factBoundTone(brainStateFact, "ok", brainStateRequestFailed),
      conclusion: brainStateKnown ? `${marketRegime} · 策略 ${strategyPosture}` : "当前态势不可确认",
      reason: sourceGaps.length ? `证据缺口 ${formatDecimal(sourceGaps.length, 0)} 个` : "自治状态快照已返回",
      next: brainStateKnown ? "交给智能体做只读分析" : "刷新态势快照",
      evidence: formatTime(pick(brainState, ["created_at"])) || "快照时间未提供",
    },
    {
      icon: BrainCircuit,
      label: "智能体分析",
      status: queryStatus(agentFactsKnown, agentFactsFailed, displayValue(agentStatus)),
      tone: queryTone(agentFactsKnown, agentFactsFailed, statusTone(agentStatus)),
      conclusion: agentFactsKnown
        ? `已登记 ${formatDecimal(pickNumber(agentAuthority, ["registered_agents"], 0), 0)} 个智能体 · ${displayValue(agentStatus) || "链路已返回"}`
        : "智能体链路不可确认",
      reason: `${countOf(pick(agentAuthority, ["unknown_sources"]))} 个未知来源 · ${countOf(pick(agentAuthority, ["contract_violations"]))} 个权限违规`,
      next: "只把合格观察送入提案链",
      evidence: agentFactsKnown ? "权限、评分与链路分开读取" : "刷新智能体状态接口",
    },
    {
      icon: Route,
      label: "提案与候选",
      status: queryStatus(agentBriefingKnown, agentBriefingRequestFailed, displayValue(pickString(proposalContextCoverage, ["status"], ""))),
      tone: queryTone(agentBriefingKnown, agentBriefingRequestFailed, proposalMissing || candidateMissing ? "warn" : "ok"),
      conclusion: agentBriefingKnown
        ? `提案上下文缺 ${proposalMissing} · 候选上下文缺 ${candidateMissing}`
        : "提案链上下文不可确认",
      reason: `交接审查缺 ${reviewMissing} · 这里只形成治理对象，不直接改运行配置`,
      next: proposalMissing || candidateMissing || reviewMissing ? "补齐上下文后再交接" : "进入风险与决策检查",
      evidence: displayContract(pickString(agentBriefing, ["schema_version"], "")) || "简报契约未提供",
    },
    {
      icon: ShieldCheck,
      label: "风险与决策",
      status: queryStatus(readinessKnown, readinessRequestFailed, blueprintStatus),
      tone: factBoundTone(readinessFact, blueprintBlockerCount ? "warn" : "ok", readinessRequestFailed),
      conclusion: readinessKnown ? `${blueprintStatus || "治理状态已返回"} · 阻断 ${blueprintBlockerCount}` : "治理就绪状态不可确认",
      reason: "保留 Safety、Readiness、Risk sizing 三层边界",
      next: blueprintBlockerCount ? "先处理阻断项，不进入执行" : "沿唯一执行路径继续",
      evidence: displayStage(pickString(v16Readiness, ["phase"], "")) || "阶段未提供",
    },
    {
      icon: UsersRound,
      label: "执行与反馈",
      status: queryStatus(liveAutonomyKnown, liveAutonomyRequestFailed, displayValue(pickString(liveAutonomy, ["autonomy_mode"], "manual"))),
      tone: factBoundTone(liveAutonomyView.fact, unlockAllowed ? "ok" : "warn", liveAutonomyRequestFailed),
      conclusion: liveAutonomyKnown
        ? `${displayValue(pickString(liveAutonomy, ["autonomy_mode"], "manual"))} · ${unlockAllowed ? "服务端评估可解锁" : "当前不允许自治解锁"}`
        : "执行边界不可确认",
      reason: `记忆反馈 ${formatDecimal(pickNumber(agentChainHealth, ["trade_feedback_summary.lesson_count"], 0), 0)} 条 · 不在此页直接下单`,
      next: unlockAllowed ? "仍需单次授权并经过实盘护栏" : "先查看阻断与护栏",
      evidence: liveAutonomyKnown ? "实盘自治状态契约" : "刷新执行边界接口",
    },
  ];

  const evidenceRows = [
    {
      icon: Network,
      label: "当前态势",
      status: queryStatus(brainStateKnown, brainStateRequestFailed, criticVerdict ? displayValue(criticVerdict) : "已返回"),
      tone: factBoundTone(brainStateFact, criticVerdict === "pass" ? "ok" : "warn", brainStateRequestFailed),
      conclusion: brainStateKnown ? `${marketRegime} · ${executionPosture}` : "当前态势不可确认",
      reason: `${readOnly ? "只读观察" : "存在执行影响"} · ${affectsTrading ? "影响交易" : "不影响交易"}`,
      next: sourceGaps.length ? "补齐证据缺口" : "继续观察并等待后验",
      evidence: formatTime(pick(brainState, ["created_at"])) || "快照时间未提供",
    },
    {
      icon: FileSearch2,
      label: "证据缺口",
      status: queryStatus(memoryKnown, memoryRequestFailed, sourceGaps.length ? "需补证" : "齐全"),
      tone: factBoundTone(memoryFact, sourceGaps.length ? "warn" : "ok", memoryRequestFailed),
      conclusion: memoryKnown ? `缺口 ${formatDecimal(sourceGaps.length, 0)} · 负面记忆 ${formatDecimal(negativeMemory.length, 0)}` : "记忆证据不可确认",
      reason: `反证 ${formatDecimal(counterEvidence.length, 0)} · 索引 ${formatDecimal(memoryItems.length, 0)}`,
      next: sourceGaps.length ? "先补证，不提升动作范围" : "进入只观察计划",
      evidence: displayContract(pickString(memory, ["schema_version"], "")) || "记忆契约未提供",
    },
    {
      icon: Route,
      label: "只观察计划",
      status: queryStatus(actionPlanKnown, actionPlanRequestFailed, actionPlans.length ? "已形成" : "暂无"),
      tone: queryTone(actionPlanKnown, actionPlanRequestFailed, actionPlans.length ? "ok" : "mute"),
      conclusion: actionPlanKnown ? `${formatDecimal(actionPlans.length, 0)} 条计划 · ${displayAction(pickString(latestActionPlan, ["action_type"], "暂无"))}` : "计划数据不可确认",
      reason: `Critic ${displayValue(pickString(latestActionPlan, ["critic_verdict"], criticVerdict)) || "未提供"} · 风险 ${displayValue(pickString(latestActionPlan, ["risk_class"], "未提供"))}`,
      next: actionPlans.length ? "等待后验评价" : "先形成只观察计划",
      evidence: formatTime(pick(latestActionPlan, ["created_at"])) || "计划时间未提供",
    },
    {
      icon: CircleDot,
      label: "后验评价",
      status: queryStatus(actionPlanEvalKnown, actionPlanEvalRequestFailed, actionPlanEvals.length ? displayValue(pickString(latestActionPlanEval, ["comparison_verdict"], "已记录")) : "暂无"),
      tone: queryTone(actionPlanEvalKnown, actionPlanEvalRequestFailed, pickString(latestActionPlanEval, ["comparison_verdict"]) === "supportive" ? "ok" : "warn"),
      conclusion: actionPlanEvalKnown ? `${formatDecimal(actionPlanEvals.length, 0)} 条评价 · 覆盖 ${formatDecimal(scorePct(pickNumber(latestActionPlanEval, ["coverage_score"], 0)), 1)}%` : "后验数据不可确认",
      reason: `回放 ${pickBoolean(asRecord(pick(latestActionPlanEval, ["comparison.source_presence"])), ["replay_report"], false) ? "有" : "无"} · 交易结果 ${pickBoolean(asRecord(pick(latestActionPlanEval, ["comparison.source_presence"])), ["trade_outcome_review"], false) ? "有" : "无"}`,
      next: actionPlanEvals.length ? "根据后验决定是否进入治理候选" : "等待评价数据",
      evidence: formatTime(pick(latestActionPlanEval, ["created_at"])) || "评价时间未提供",
    },
    {
      icon: Play,
      label: "低影响执行",
      status: queryStatus(lowImpactKnown, lowImpactRequestFailed, lowImpactExecutions.length ? displayValue(pickString(latestLowImpactExecution, ["status"], "已记录")) : "暂无"),
      tone: queryTone(lowImpactKnown, lowImpactRequestFailed, lowImpactExecutions.length ? (pickString(latestLowImpactExecution, ["status"]).includes("blocked") ? "warn" : "ok") : "mute"),
      conclusion: lowImpactKnown ? `${formatDecimal(lowImpactExecutions.length, 0)} 条回放 · ${displayAction(pickString(latestLowImpactExecution, ["execution_action"], "暂无"))}` : "执行数据不可确认",
      reason: pickString(asRecord(pick(latestLowImpactExecution, ["risk_verdict"])), ["reason"], "只读回放不改变线上权限"),
      next: "保留后验；未经治理审核不写回线上",
      evidence: formatTime(pick(latestLowImpactExecution, ["created_at"])) || "执行时间未提供",
    },
  ];

  return (
    <section className="dashboard v16-dashboard">
      {!embedded ? <div className="dashboard-header">
        <div>
          <div className="eyebrow">自治决策系统</div>
          <h1>自治治理链路</h1>
          <p>运行日志、待治理链路、依据反馈和执行边界。</p>
        </div>
        <div className="header-status">
          <StatusPill status={displayValue(pickString(autonomousBlueprint, ["status"], ""))} tone={factBoundTone(readinessFact, pickBoolean(autonomousBlueprint, ["ok"], false) ? "ok" : "warn", readinessRequestFailed)} />
          <StatusPill status={displayStage(pickString(v16Readiness, ["phase"], ""))} tone={factBoundTone(readinessFact, "ok", readinessRequestFailed)} />
          {activeTab === "overview" || activeTab === "evidence" ? (
            <StatusPill status={brainStateKnown ? (readOnly && !affectsTrading ? "交易边界正常" : "边界异常") : "交易边界未知"} tone={factBoundTone(brainStateFact, readOnly && !affectsTrading ? "ok" : "bad", brainStateRequestFailed)} />
          ) : null}
          <button className="header-refresh" type="button" disabled={refreshMutation.isPending} onClick={() => refreshMutation.mutate()}>
            <RefreshCw size={15} aria-hidden="true" />
            {refreshMutation.isPending ? "刷新中" : "刷新治理状态"}
          </button>
          {refreshMutation.isError ? <span className="error-text small">刷新失败</span> : null}
          {lowImpactMutation.isError ? <span className="error-text small">低影响回放失败</span> : null}
          {mediumImpactMutation.isError ? <span className="error-text small">治理候选失败</span> : null}
          {candidateReviewMutation.isError ? <span className="error-text small">候选审查失败</span> : null}
          {liveReadyEvaluateMutation.isError ? <span className="error-text small">护栏评估失败</span> : null}
          {liveReadyTightenMutation.isError ? <span className="error-text small">收紧失败</span> : null}
        </div>
      </div> : null}

      <nav className="section-tabs" aria-label="自治治理分区">
        {chainTabs.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.key}
              className={`section-tab ${activeTab === tab.key ? "section-tab-active" : ""}`}
              type="button"
              aria-current={activeTab === tab.key ? "page" : undefined}
              onClick={() => setActiveTab(tab.key)}
            >
              <Icon size={15} aria-hidden="true" />{tab.label}
            </button>
          );
        })}
      </nav>

      {activeTab === "overview" ? (
        <div className="dashboard-grid v16-grid v16-overview-grid">
          <MetricCard title="运行日志：输入 → 裁决 → 风控 → 执行 → 反馈" className="wide-panel">
            <RuntimeLog rows={runtimeRows} />
          </MetricCard>
          <details className="detail-disclosure wide-panel v16-overview-disclosure">
            <summary><UsersRound size={15} aria-hidden="true" />查看智能体权限明细（默认收起）</summary>
            <MetricCard title="智能体权限" className="v16-overview-detail">
              <div className={agentFactsKnown && agentScorecardKnown && agentBriefingKnown ? "" : "fact-unverified"}>
                <AgentAuthorityPanel agentAuthority={agentAuthority} agentScorecard={agentScorecard} agentBriefing={agentBriefing} chainHealth={agentChainHealth} />
              </div>
            </MetricCard>
          </details>
        </div>
      ) : null}

      {activeTab === "proposals" ? (
        <div className="dashboard-grid v16-grid">
          <MetricCard title="待治理链路：提案 → 候选 → 审查" className="wide-panel">
            <div className={`v16-boundary ${proposalRegistryKnown ? "" : "fact-unverified"}`.trim()}>
              <Field label="提案" value={formatDecimal(pickNumber(proposalSummary, ["proposal_count"], proposalItems.length), 0)} />
              <Field label="活跃" value={formatDecimal(pickNumber(proposalSummary, ["active_count"], 0), 0)} />
              <Field label="冲突" value={formatDecimal(pickNumber(proposalSummary, ["conflict_count"], 0), 0)} tone={factBoundTone(proposalRegistryFact, pickNumber(proposalSummary, ["conflict_count"], 0) ? "warn" : "ok", proposalRegistryRequestFailed)} />
              <Field label="高危未解" value={formatDecimal(pickNumber(proposalSummary, ["high_unresolved_conflict_count"], 0), 0)} tone={factBoundTone(proposalRegistryFact, pickNumber(proposalSummary, ["high_unresolved_conflict_count"], 0) ? "bad" : "ok", proposalRegistryRequestFailed)} />
            </div>
            <details className="detail-disclosure v16-inline-disclosure">
              <summary>展开上下文覆盖与交接检查（主链不重复显示）</summary>
              <div className={agentBriefingKnown ? "" : "fact-unverified"}>
                <CoveragePanel proposalContext={proposalContextCoverage} candidateContext={candidateContextCoverage} candidateReview={candidateBridgeReviewCoverage} />
              </div>
            </details>
            <div className="brain-card-actions">
              <button className="header-refresh" type="button" disabled={proposalRefreshMutation.isPending} onClick={() => proposalRefreshMutation.mutate()}><RefreshCw size={15} />{proposalRefreshMutation.isPending ? "刷新中" : "刷新提案"}</button>
              <button className="header-refresh" type="button" disabled={mediumImpactMutation.isPending} onClick={() => mediumImpactMutation.mutate()}><ListChecks size={15} />{mediumImpactMutation.isPending ? "生成中" : "生成候选"}</button>
              <button className="header-refresh" type="button" disabled={candidateReviewMutation.isPending} onClick={() => candidateReviewMutation.mutate()}><GitBranch size={15} />{candidateReviewMutation.isPending ? "审查中" : "运行候选审查"}</button>
            </div>
            <GovernancePipeline
              proposalItems={proposalItems}
              governanceItems={mediumImpactGovernance}
              reviewItems={candidateReviews}
              proposalFact={{ state: proposalRegistryFact.state, failed: proposalRegistryRequestFailed }}
              governanceFact={{ state: mediumImpactFact.state, failed: mediumImpactRequestFailed }}
              reviewFact={{ state: candidateReviewFact.state, failed: candidateReviewRequestFailed }}
              reviewing={proposalReviewMutation.isPending}
              onReview={(proposalId, route) => proposalReviewMutation.mutate({ proposalId, route })}
            />
          </MetricCard>
        </div>
      ) : null}

      {activeTab === "evidence" ? (
        <div className="dashboard-grid v16-grid">
          <MetricCard title="依据与反馈：当前结论 → 证据缺口 → 后验" className="wide-panel">
            <div className={`v16-evidence-summary ${brainStateKnown ? "" : "fact-unverified"}`.trim()}>
              <CompactMetric label="市场状态" value={displayValue(pickString(worldModel, ["market_regime"], ""))} />
              <CompactMetric label="建议策略" value={displayValue(pickString(worldModel, ["strategy_posture"], ""))} tone={factBoundTone(brainStateFact, statTone, brainStateRequestFailed)} />
              <CompactMetric label="执行状态" value={displayValue(pickString(worldModel, ["execution_posture"], ""))} tone={factBoundTone(brainStateFact, toneFromStatus(pickString(worldModel, ["execution_posture"], "")), brainStateRequestFailed)} />
              <CompactMetric label="证据审查" value={displayValue(criticVerdict)} detail={displayValue(pickString(critic, ["max_allowed_action_scope"], ""))} tone={factBoundTone(brainStateFact, criticVerdict === "pass" ? "ok" : "warn", brainStateRequestFailed)} />
            </div>
            <div className="v16-boundary">
              <Field label="运行方式" value={readOnly ? "只读观察" : "可执行"} tone={factBoundTone(brainStateFact, boolTone(readOnly), brainStateRequestFailed)} />
              <Field label="交易权限" value={affectsTrading ? "会影响交易" : "不影响交易"} tone={factBoundTone(brainStateFact, affectsTrading ? "bad" : "ok", brainStateRequestFailed)} />
              <Field label="证据缺口" value={formatDecimal(sourceGaps.length, 0)} tone={factBoundTone(memoryFact, sourceGaps.length ? "warn" : "ok", memoryRequestFailed)} />
              <Field label="最近快照" value={formatTime(pick(brainState, ["created_at"]))} />
            </div>
            <RuntimeLog rows={evidenceRows} />
            <div className="brain-card-actions">
              <button className="header-refresh" type="button" disabled={lowImpactMutation.isPending} onClick={() => lowImpactMutation.mutate()}><Play size={15} />{lowImpactMutation.isPending ? "运行中" : "运行只读回放"}</button>
            </div>
            <details className="detail-disclosure v16-inline-disclosure">
              <summary><FileSearch2 size={15} aria-hidden="true" />展开记忆、计划、评价与执行明细（主链只显示结论）</summary>
              <div className="v16-detail-grid">
                <section className="v16-detail-section">
                  <SectionHead title="假设与负面记忆" status={`${hypotheses.length + negativeMemory.length}`} tone={factBoundTone(brainStateFact, hypotheses.length || negativeMemory.length ? "warn" : "mute", brainStateRequestFailed)} />
                  <div className={brainStateKnown ? "" : "fact-unverified"}><HypothesisList items={hypotheses} /></div>
                  <div className={memoryKnown ? "" : "fact-unverified"}><MemoryList items={negativeMemory} empty="暂无负面记忆" /></div>
                </section>
                <section className="v16-detail-section">
                  <SectionHead title="反证与最近索引" status={`${counterEvidence.length + memoryItems.length}`} tone={factBoundTone(memoryFact, counterEvidence.length ? "warn" : "mute", memoryRequestFailed)} />
                  <div className={memoryKnown ? "" : "fact-unverified"}><MemoryList items={counterEvidence} empty="暂无反证" /></div>
                  <div className={memoryKnown ? "" : "fact-unverified"}><MemoryList items={memoryItems.slice(0, 8)} empty="暂无索引记忆" /></div>
                </section>
                <section className="v16-detail-section">
                  <SectionHead title="只观察计划" status={`${actionPlans.length}`} tone={factBoundTone(actionPlanFact, actionPlans.length ? "ok" : "mute", actionPlanRequestFailed)} />
                  <div className={actionPlanKnown ? "" : "fact-unverified"}><ActionPlanList items={actionPlans} /></div>
                  <SectionHead title="后验评价" status={`${actionPlanEvals.length}`} tone={factBoundTone(actionPlanEvalFact, actionPlanEvals.length ? "ok" : "mute", actionPlanEvalRequestFailed)} />
                  <div className={actionPlanEvalKnown ? "" : "fact-unverified"}><EvaluationList items={actionPlanEvals} /></div>
                </section>
                <section className="v16-detail-section">
                  <SectionHead title="低影响执行" status={`${lowImpactExecutions.length}`} tone={factBoundTone(lowImpactFact, lowImpactExecutions.length ? "ok" : "mute", lowImpactRequestFailed)} />
                  <div className={lowImpactKnown ? "" : "fact-unverified"}><ExecutionList items={lowImpactExecutions} /></div>
                </section>
              </div>
            </details>
            <details className="detail-disclosure v16-inline-disclosure">
              <summary><FileSearch2 size={15} aria-hidden="true" />查看完整证据引用</summary>
              <JsonBlock value={{ evidence_refs: evidenceRefs, query_terms: pickArray(memory, ["query_terms"]), source_gaps: sourceGaps }} />
            </details>
          </MetricCard>
        </div>
      ) : null}

      {activeTab === "control" ? (
        <div className="dashboard-grid v16-grid">
          <MetricCard title="执行边界：能否执行 / 当前护栏" className="wide-panel">
            <div className="v16-control-lead">
              <div>
                <span>当前结论</span>
                <strong>{liveAutonomyKnown ? (unlockAllowed ? "服务端评估允许一次解锁" : "当前不允许自治解锁") : "无法确认是否允许执行"}</strong>
                <small>{liveAutonomyKnown ? `${displayValue(pickString(liveAutonomy, ["autonomy_mode"], "manual"))} · 阻断 ${formatDecimal(liveAutonomyBlockers.length, 0)} 项` : "执行边界事实未确认，不能把缓存值当成当前权限"}</small>
              </div>
              <StatusPill status={liveAutonomyKnown ? (unlockAllowed ? "可评估解锁" : "保持锁定") : "数据待确认"} tone={factBoundTone(liveAutonomyView.fact, unlockAllowed ? "ok" : "warn", liveAutonomyRequestFailed)} />
            </div>
            <FactBoundary fact={liveAutonomyView.fact} label="实盘自治事实">
              <div className="v16-boundary">
                <Field label="模式" value={displayValue(pickString(liveAutonomy, ["autonomy_mode"], "manual"))} tone={factBoundTone(liveAutonomyView.fact, pickBoolean(liveAutonomy, ["live_autonomy_unlocked"], false) ? "ok" : "warn", liveAutonomyRequestFailed)} />
                <Field label="评估" value={displayValue(pickString(liveAutonomyEvaluation, ["status"], "blocked"))} tone={factBoundTone(liveAutonomyView.fact, unlockAllowed ? "ok" : "warn", liveAutonomyRequestFailed)} />
                <Field label="阻断项" value={formatDecimal(liveAutonomyBlockers.length, 0)} tone={factBoundTone(liveAutonomyView.fact, liveAutonomyBlockers.length ? "warn" : "ok", liveAutonomyRequestFailed)} />
                <Field label="建议模式" value={displayValue(pickString(liveAutonomyPosture, ["recommended_incident_mode"], "normal"))} />
              </div>
            </FactBoundary>
            <div className={`v16-boundary v16-control-guardrail-facts ${liveReadyGuardrailKnown ? "" : "fact-unverified"}`.trim()}>
              <Field label="能力锁" value={displayValue(pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "locked" : "unlocked")} tone={factBoundTone(liveReadyGuardrailFact, pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "ok" : "warn", liveReadyGuardrailRequestFailed)} />
              <Field label="经纪商偏差" value={displayValue(pickString(latestGuardrail, ["broker_local_divergence.status"], ""))} />
              <Field label="事故模式" value={displayValue(pickString(latestGuardrail, ["incident_control.mode"], ""))} />
              <Field label="回滚" value={displayValue(pickBoolean(latestGuardrail, ["release_rollback.rollback_ready"], false) ? "ready" : "missing")} />
            </div>
            <div className="brain-card-actions">
              <button className="header-refresh" type="button" disabled={liveUnlockEvaluateMutation.isPending} onClick={() => liveUnlockEvaluateMutation.mutate()}><ShieldCheck size={15} />{liveUnlockEvaluateMutation.isPending ? "评估中" : "评估解锁"}</button>
              <ActionButton icon={ShieldCheck} label="一次解锁" variant="danger" disabled={liveUnlockMutation.isPending || !unlockAllowed} loading={liveUnlockMutation.isPending} confirmTitle="确认解锁实盘自治" confirmMessage="只有服务端评估通过且事实仍新鲜时才能执行。" stepUpOnDemand onAction={runLiveUnlock} />
              <ActionButton icon={ShieldCheck} label="撤销自治" variant="danger" disabled={liveRevokeMutation.isPending} loading={liveRevokeMutation.isPending} confirmMessage="撤销后实盘自治立即回到锁定状态。" onAction={() => liveRevokeMutation.mutateAsync()} />
              <button className="header-refresh" type="button" disabled={liveReadyEvaluateMutation.isPending} onClick={() => liveReadyEvaluateMutation.mutate()}><ShieldCheck size={15} />{liveReadyEvaluateMutation.isPending ? "评估中" : "评估护栏"}</button>
              <ActionButton icon={ShieldCheck} label="不增风险" variant="ghost" disabled={liveReadyTightenMutation.isPending} onAction={() => liveReadyTightenMutation.mutateAsync("no_new_risk")} />
              <ActionButton icon={ShieldCheck} label="仅平仓" variant="danger" disabled={liveReadyTightenMutation.isPending} onAction={() => liveReadyTightenMutation.mutateAsync("only_close")} />
              <ActionButton icon={ShieldCheck} label="冻结" variant="danger" disabled={liveReadyTightenMutation.isPending} onAction={() => liveReadyTightenMutation.mutateAsync("frozen")} />
            </div>
            <details className="detail-disclosure v16-inline-disclosure">
              <summary><ShieldCheck size={15} aria-hidden="true" />展开阻断项与最近护栏记录（控制动作仍受服务端事实约束）</summary>
              <div className="v16-detail-grid v16-control-detail-grid">
                <section className={`v16-detail-section ${liveAutonomyKnown ? "" : "fact-unverified"}`.trim()}>
                  <SectionHead title="自治解锁阻断" status={`${liveAutonomyBlockers.length}`} tone={factBoundTone(liveAutonomyView.fact, liveAutonomyBlockers.length ? "warn" : "ok", liveAutonomyRequestFailed)} />
                  <div className="brain-list">
                    {liveAutonomyBlockers.slice(0, 8).map((raw, index) => {
                      const item = asRecord(raw);
                      const component = pickString(item, ["component"], "unlock");
                      const status = pickString(item, ["status"], pickString(item, ["reason"], "blocked"));
                      return <div className="brain-list-row" key={`${component}-${index}`}><div><strong>{displayValue(component)}</strong><span>{displayValue(pickString(item, ["reason"], status))}</span></div><StatusPill status={displayValue(status)} tone="warn" /></div>;
                    })}
                    {!liveAutonomyBlockers.length ? <div className="empty-state-small">当前没有返回自治阻断项</div> : null}
                  </div>
                </section>
                <section className={`v16-detail-section ${liveReadyGuardrailKnown ? "" : "fact-unverified"}`.trim()}>
                  <SectionHead title="护栏记录" status={`${liveReadyGuardrails.length}`} tone={factBoundTone(liveReadyGuardrailFact, liveReadyGuardrails.length ? "ok" : "warn", liveReadyGuardrailRequestFailed)} />
                  <GuardrailList items={liveReadyGuardrails} />
                </section>
              </div>
            </details>
          </MetricCard>
        </div>
      ) : null}
    </section>
  );
}
