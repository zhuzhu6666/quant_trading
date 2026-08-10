import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BrainCircuit,
  Database,
  FileSearch2,
  GitBranch,
  ListChecks,
  Network,
  Play,
  RefreshCw,
  Route,
  ShieldCheck,
  Sparkles,
  SlidersHorizontal,
  UsersRound,
  Workflow,
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
import { CompactMetric, Field, SectionHead, StatTile, toneFromStatus, type Tone } from "@/components/DashboardBits";
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
  BlueprintOverview,
  CandidateReviewList,
  CoveragePanel,
  EvaluationList,
  ExecutionList,
  GovernanceList,
  GuardrailList,
  HypothesisList,
  MemoryList,
  ProposalRegistryList,
  asRecord,
  boolTone,
  countOf,
  displayContract,
  displayStage,
  displayValue,
  formatTime,
  pick,
  pickArray,
  pickBoolean,
  pickNumber,
  pickString,
  statusTone,
} from "@/features/v16/V16BrainViews";

type ChainTab = "overview" | "proposals" | "evidence" | "control";

const chainTabs: Array<{ key: ChainTab; label: string; icon: typeof Network }> = [
  { key: "overview", label: "链路总览", icon: Network },
  { key: "proposals", label: "待审建议", icon: GitBranch },
  { key: "evidence", label: "依据与反馈", icon: FileSearch2 },
  { key: "control", label: "实盘控制", icon: SlidersHorizontal },
];

export function V16BrainPage({ embedded = false }: { embedded?: boolean }) {
  const [activeTab, setActiveTab] = useState<ChainTab>("overview");
  const queryClient = useQueryClient();
  const readinessQuery = useBackendReadinessQuery();
  const agentAuthorityQuery = useQuery({ queryKey: ["v16", "agent-authority"], queryFn: getAgentAuthority, enabled: activeTab === "overview", refetchInterval: 30_000, staleTime: 10_000 });
  const agentScorecardQuery = useQuery({ queryKey: ["v16", "agent-scorecard"], queryFn: () => getAgentScorecard(300), enabled: activeTab === "overview", refetchInterval: 30_000, staleTime: 10_000 });
  const agentBriefingQuery = useQuery({ queryKey: ["v16", "agent-briefing"], queryFn: () => getAgentBriefing(20), enabled: activeTab === "overview", refetchInterval: 30_000, staleTime: 10_000 });
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
        await getBrainMediumImpactGovernance(24);
        await getBrainGovernanceCandidateReviews(24);
        await getAutonomyProposals(true, 24);
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
  const agentScorecard = asRecord(pick(agentScorecardQuery.data, ["scorecard"]));
  const agentBriefing = asRecord(pick(agentBriefingQuery.data, ["briefing"]));
  const agentChainHealth = asRecord(
    pick(agentChainHealthQuery.data, ["agent_chain_health"]),
  );
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

  return (
    <section className="dashboard v16-dashboard">
      {!embedded ? <div className="dashboard-header">
        <div>
          <div className="eyebrow">自治决策系统</div>
          <h1>自治治理链路</h1>
          <p>多智能体权责、提案总线、候选审查、记忆反馈和执行边界。</p>
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
        <div className="dashboard-grid v16-grid">
          <MetricCard title="治理链路总览" className="wide-panel">
            <div className={readinessKnown ? "" : "fact-unverified"}>
              <BlueprintOverview
                blueprint={autonomousBlueprint}
                agentAuthority={agentAuthority}
                proposalContext={proposalContextCoverage}
                candidateContext={candidateContextCoverage}
                candidateReview={candidateBridgeReviewCoverage}
                chainHealth={agentChainHealth}
                liveAutonomy={liveAutonomy}
              />
            </div>
          </MetricCard>
          <MetricCard title="智能体权限" className="v16-overview-detail">
            <div className={readinessKnown ? "" : "fact-unverified"}>
              <AgentAuthorityPanel agentAuthority={agentAuthority} agentScorecard={agentScorecard} agentBriefing={agentBriefing} chainHealth={agentChainHealth} />
            </div>
          </MetricCard>
          <MetricCard title="决策信息完整度" className="v16-overview-detail">
            <div className={readinessKnown ? "" : "fact-unverified"}>
              <CoveragePanel proposalContext={proposalContextCoverage} candidateContext={candidateContextCoverage} candidateReview={candidateBridgeReviewCoverage} />
            </div>
          </MetricCard>
          <MetricCard title="系统态势与证据审查" className="wide-panel">
            <div className={`brain-mini-grid ${brainStateKnown ? "" : "fact-unverified"}`.trim()}>
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
          </MetricCard>
        </div>
      ) : null}

      {activeTab === "proposals" ? (
        <div className="dashboard-grid v16-grid">
          <MetricCard title="治理提案总线" className="wide-panel">
            <div className={`v16-boundary ${proposalRegistryKnown ? "" : "fact-unverified"}`.trim()}>
              <Field label="提案" value={formatDecimal(pickNumber(proposalSummary, ["proposal_count"], proposalItems.length), 0)} />
              <Field label="活跃" value={formatDecimal(pickNumber(proposalSummary, ["active_count"], 0), 0)} />
              <Field label="冲突" value={formatDecimal(pickNumber(proposalSummary, ["conflict_count"], 0), 0)} tone={factBoundTone(proposalRegistryFact, pickNumber(proposalSummary, ["conflict_count"], 0) ? "warn" : "ok", proposalRegistryRequestFailed)} />
              <Field label="高危未解" value={formatDecimal(pickNumber(proposalSummary, ["high_unresolved_conflict_count"], 0), 0)} tone={factBoundTone(proposalRegistryFact, pickNumber(proposalSummary, ["high_unresolved_conflict_count"], 0) ? "bad" : "ok", proposalRegistryRequestFailed)} />
            </div>
            <div className="brain-card-actions">
              <button className="header-refresh" type="button" disabled={proposalRefreshMutation.isPending} onClick={() => proposalRefreshMutation.mutate()}><RefreshCw size={15} />{proposalRefreshMutation.isPending ? "刷新中" : "刷新提案"}</button>
            </div>
            <div className={proposalRegistryKnown ? "" : "fact-unverified"}>
              <ProposalRegistryList items={proposalItems} reviewing={proposalReviewMutation.isPending} onReview={(proposalId, route) => proposalReviewMutation.mutate({ proposalId, route })} />
            </div>
          </MetricCard>
          <MetricCard title="治理候选">
            <div className="brain-card-actions">
              <button className="header-refresh" type="button" disabled={mediumImpactMutation.isPending} onClick={() => mediumImpactMutation.mutate()}><ListChecks size={15} />{mediumImpactMutation.isPending ? "生成中" : "生成候选"}</button>
            </div>
            <div className={mediumImpactKnown ? "" : "fact-unverified"}><GovernanceList items={mediumImpactGovernance} /></div>
          </MetricCard>
          <MetricCard title="候选审查">
            <div className="brain-card-actions">
              <button className="header-refresh" type="button" disabled={candidateReviewMutation.isPending} onClick={() => candidateReviewMutation.mutate()}><GitBranch size={15} />{candidateReviewMutation.isPending ? "审查中" : "审查候选"}</button>
            </div>
            <div className={candidateReviewKnown ? "" : "fact-unverified"}><CandidateReviewList items={candidateReviews} /></div>
          </MetricCard>
        </div>
      ) : null}

      {activeTab === "evidence" ? (
        <div className="dashboard-grid v16-grid">
          <MetricCard title="记忆与假设">
            <SectionHead title="假设" status={`${hypotheses.length}`} tone={factBoundTone(brainStateFact, hypotheses.length ? "ok" : "warn", brainStateRequestFailed)} />
            <div className={brainStateKnown ? "" : "fact-unverified"}><HypothesisList items={hypotheses} /></div>
            <SectionHead title="负面记忆" status={`${negativeMemory.length}`} tone={factBoundTone(memoryFact, negativeMemory.length ? "warn" : "ok", memoryRequestFailed)} />
            <div className={memoryKnown ? "" : "fact-unverified"}><MemoryList items={negativeMemory} empty="暂无负面记忆" /></div>
          </MetricCard>
          <MetricCard title="反证反馈">
            <SectionHead title="反证" status={`${counterEvidence.length}`} tone={factBoundTone(memoryFact, counterEvidence.length ? "ok" : "mute", memoryRequestFailed)} />
            <div className={memoryKnown ? "" : "fact-unverified"}><MemoryList items={counterEvidence} empty="暂无反证" /></div>
            <SectionHead title="最近索引" status={`${memoryItems.length}`} />
            <div className={memoryKnown ? "" : "fact-unverified"}><MemoryList items={memoryItems.slice(0, 8)} empty="暂无索引记忆" /></div>
          </MetricCard>
          <MetricCard title="只观察计划">
            <div className={actionPlanKnown ? "" : "fact-unverified"}><ActionPlanList items={actionPlans} /></div>
          </MetricCard>
          <MetricCard title="后验评价">
            <div className={actionPlanEvalKnown ? "" : "fact-unverified"}><EvaluationList items={actionPlanEvals} /></div>
          </MetricCard>
          <MetricCard title="低影响执行" className="wide-panel">
            <div className="brain-card-actions">
              <button className="header-refresh" type="button" disabled={lowImpactMutation.isPending} onClick={() => lowImpactMutation.mutate()}><Play size={15} />{lowImpactMutation.isPending ? "运行中" : "运行只读回放"}</button>
            </div>
            <div className={lowImpactKnown ? "" : "fact-unverified"}><ExecutionList items={lowImpactExecutions} /></div>
          </MetricCard>
          <details className="detail-disclosure wide-panel">
            <summary><FileSearch2 size={15} />查看完整证据引用</summary>
            <JsonBlock value={{ evidence_refs: evidenceRefs, query_terms: pickArray(memory, ["query_terms"]), source_gaps: sourceGaps }} />
          </details>
        </div>
      ) : null}

      {activeTab === "control" ? (
        <div className="dashboard-grid v16-grid">
          <MetricCard title="实盘自治" className="wide-panel">
            <FactBoundary fact={liveAutonomyView.fact} label="实盘自治事实">
              <div className="v16-boundary">
                <Field label="模式" value={displayValue(pickString(liveAutonomy, ["autonomy_mode"], "manual"))} tone={factBoundTone(liveAutonomyView.fact, pickBoolean(liveAutonomy, ["live_autonomy_unlocked"], false) ? "ok" : "warn", liveAutonomyRequestFailed)} />
                <Field label="评估" value={displayValue(pickString(liveAutonomyEvaluation, ["status"], "blocked"))} tone={factBoundTone(liveAutonomyView.fact, unlockAllowed ? "ok" : "warn", liveAutonomyRequestFailed)} />
                <Field label="阻断项" value={formatDecimal(liveAutonomyBlockers.length, 0)} tone={factBoundTone(liveAutonomyView.fact, liveAutonomyBlockers.length ? "warn" : "ok", liveAutonomyRequestFailed)} />
                <Field label="建议模式" value={displayValue(pickString(liveAutonomyPosture, ["recommended_incident_mode"], "normal"))} />
              </div>
            </FactBoundary>
            <div className="brain-card-actions">
              <button className="header-refresh" type="button" disabled={liveUnlockEvaluateMutation.isPending} onClick={() => liveUnlockEvaluateMutation.mutate()}><ShieldCheck size={15} />{liveUnlockEvaluateMutation.isPending ? "评估中" : "评估解锁"}</button>
              <ActionButton icon={ShieldCheck} label="一次解锁" variant="danger" disabled={liveUnlockMutation.isPending || !unlockAllowed} loading={liveUnlockMutation.isPending} confirmTitle="确认解锁实盘自治" confirmMessage="只有服务端评估通过且事实仍新鲜时才能执行。" stepUpOnDemand onAction={runLiveUnlock} />
              <ActionButton icon={ShieldCheck} label="撤销自治" variant="danger" disabled={liveRevokeMutation.isPending} loading={liveRevokeMutation.isPending} confirmMessage="撤销后实盘自治立即回到锁定状态。" onAction={() => liveRevokeMutation.mutateAsync()} />
            </div>
            <div className={`brain-list ${liveAutonomyKnown ? "" : "fact-unverified"}`.trim()}>
              {liveAutonomyBlockers.slice(0, 8).map((raw, index) => {
                const item = asRecord(raw);
                const component = pickString(item, ["component"], "unlock");
                const status = pickString(item, ["status"], pickString(item, ["reason"], "blocked"));
                return <div className="brain-list-row" key={`${component}-${index}`}><div><strong>{displayValue(component)}</strong><span>{displayValue(pickString(item, ["reason"], status))}</span></div><StatusPill status={displayValue(status)} tone="warn" /></div>;
              })}
            </div>
          </MetricCard>
          <MetricCard title="实盘护栏" className="wide-panel">
            <div className={`v16-boundary ${liveReadyGuardrailKnown ? "" : "fact-unverified"}`.trim()}>
              <Field label="能力锁" value={displayValue(pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "locked" : "unlocked")} tone={factBoundTone(liveReadyGuardrailFact, pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "ok" : "warn", liveReadyGuardrailRequestFailed)} />
              <Field label="偏差" value={displayValue(pickString(latestGuardrail, ["broker_local_divergence.status"], ""))} />
              <Field label="事故" value={displayValue(pickString(latestGuardrail, ["incident_control.mode"], ""))} />
              <Field label="回滚" value={displayValue(pickBoolean(latestGuardrail, ["release_rollback.rollback_ready"], false) ? "ready" : "missing")} />
            </div>
            <div className="brain-card-actions">
              <button className="header-refresh" type="button" disabled={liveReadyEvaluateMutation.isPending} onClick={() => liveReadyEvaluateMutation.mutate()}><ShieldCheck size={15} />{liveReadyEvaluateMutation.isPending ? "评估中" : "评估护栏"}</button>
              <ActionButton icon={ShieldCheck} label="不增风险" variant="ghost" disabled={liveReadyTightenMutation.isPending} onAction={() => liveReadyTightenMutation.mutateAsync("no_new_risk")} />
              <ActionButton icon={ShieldCheck} label="仅平仓" variant="danger" disabled={liveReadyTightenMutation.isPending} onAction={() => liveReadyTightenMutation.mutateAsync("only_close")} />
              <ActionButton icon={ShieldCheck} label="冻结" variant="danger" disabled={liveReadyTightenMutation.isPending} onAction={() => liveReadyTightenMutation.mutateAsync("frozen")} />
            </div>
            <div className={liveReadyGuardrailKnown ? "" : "fact-unverified"}><GuardrailList items={liveReadyGuardrails} /></div>
          </MetricCard>
        </div>
      ) : null}
    </section>
  );
}
