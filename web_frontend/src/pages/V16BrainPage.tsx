import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BrainCircuit,
  Database,
  GitBranch,
  ListChecks,
  Network,
  Play,
  RefreshCw,
  Route,
  ShieldCheck,
  Sparkles,
  UsersRound,
  Workflow,
} from "lucide-react";
import {
  evaluateLiveAutonomyUnlock,
  evaluateBrainLiveReadyGuardrail,
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

export function V16BrainPage() {
  const queryClient = useQueryClient();
  const readinessQuery = useBackendReadinessQuery();
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
  const directBrainState = asRecord(pick(brainStateQuery.data, ["brain_state"]));
  const hasDirectBrainState = Object.keys(directBrainState).length > 0;
  const readinessBrainState = asRecord(pick(v16Readiness, ["brain_state.latest_snapshot"]));
  const brainState = hasDirectBrainState ? directBrainState : readinessBrainState;
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
  const directActionPlanRun = asRecord(pick(brainActionPlansQuery.data, ["action_plans"]));
  const hasDirectActionPlanRun = Object.keys(directActionPlanRun).length > 0;
  const readinessActionPlans = asRecord(pick(v16Readiness, ["action_plans"]));
  const actionPlanRun = hasDirectActionPlanRun ? directActionPlanRun : readinessActionPlans;
  const actionPlanFact = readFact(brainActionPlansQuery.data, "ops.v16-action-plans.v2");
  const actionPlanRequestFailed = brainActionPlansQuery.isError || brainActionPlansQuery.isRefetchError;
  const actionPlanKnown = factIsKnown(actionPlanFact, actionPlanRequestFailed);
  const actionPlans = pickArray(actionPlanRun, ["plans"]);
  const directActionPlanEvals = asRecord(pick(brainActionPlanEvalsQuery.data, ["action_plan_evals"]));
  const hasDirectActionPlanEvals = Object.keys(directActionPlanEvals).length > 0;
  const readinessActionPlanEvals = asRecord(pick(v16Readiness, ["action_plan_evals"]));
  const actionPlanEvalRun = hasDirectActionPlanEvals ? directActionPlanEvals : readinessActionPlanEvals;
  const actionPlanEvalFact = readFact(brainActionPlanEvalsQuery.data, "ops.v16-action-plan-evals.v2");
  const actionPlanEvalRequestFailed = brainActionPlanEvalsQuery.isError || brainActionPlanEvalsQuery.isRefetchError;
  const actionPlanEvalKnown = factIsKnown(actionPlanEvalFact, actionPlanEvalRequestFailed);
  const actionPlanEvals = pickArray(actionPlanEvalRun, ["evals"]);
  const directLowImpactExecutions = asRecord(pick(lowImpactExecutionsQuery.data, ["low_impact_executions"]));
  const hasDirectLowImpactExecutions = Object.keys(directLowImpactExecutions).length > 0;
  const readinessLowImpactExecutions = asRecord(pick(v16Readiness, ["low_impact_executions"]));
  const lowImpactExecutionRun = hasDirectLowImpactExecutions ? directLowImpactExecutions : readinessLowImpactExecutions;
  const lowImpactFact = readFact(lowImpactExecutionsQuery.data, "ops.v16-low-impact-executions.v2");
  const lowImpactRequestFailed = lowImpactExecutionsQuery.isError || lowImpactExecutionsQuery.isRefetchError;
  const lowImpactKnown = factIsKnown(lowImpactFact, lowImpactRequestFailed);
  const lowImpactExecutions = pickArray(lowImpactExecutionRun, ["executions"]);
  const directMediumImpactGovernance = asRecord(pick(mediumImpactGovernanceQuery.data, ["medium_impact_governance"]));
  const hasDirectMediumImpactGovernance = Object.keys(directMediumImpactGovernance).length > 0;
  const readinessMediumImpactGovernance = asRecord(pick(v16Readiness, ["medium_impact_governance"]));
  const mediumImpactGovernanceRun = hasDirectMediumImpactGovernance ? directMediumImpactGovernance : readinessMediumImpactGovernance;
  const mediumImpactFact = readFact(mediumImpactGovernanceQuery.data, "ops.v16-medium-impact-governance.v2");
  const mediumImpactRequestFailed = mediumImpactGovernanceQuery.isError || mediumImpactGovernanceQuery.isRefetchError;
  const mediumImpactKnown = factIsKnown(mediumImpactFact, mediumImpactRequestFailed);
  const mediumImpactGovernance = pickArray(mediumImpactGovernanceRun, ["items"]);
  const directCandidateReviews = asRecord(pick(candidateReviewsQuery.data, ["candidate_reviews"]));
  const hasDirectCandidateReviews = Object.keys(directCandidateReviews).length > 0;
  const readinessCandidateReviews = asRecord(pick(v16Readiness, ["governance_candidate_reviews"]));
  const candidateReviewRun = hasDirectCandidateReviews ? directCandidateReviews : readinessCandidateReviews;
  const candidateReviewFact = readFact(candidateReviewsQuery.data, "ops.v16-governance-candidate-reviews.v2");
  const candidateReviewRequestFailed = candidateReviewsQuery.isError || candidateReviewsQuery.isRefetchError;
  const candidateReviewKnown = factIsKnown(candidateReviewFact, candidateReviewRequestFailed);
  const candidateReviews = pickArray(candidateReviewRun, ["items"]);
  const directLiveReadyGuardrails = asRecord(pick(liveReadyGuardrailsQuery.data, ["live_ready_guardrails"]));
  const hasDirectLiveReadyGuardrails = Object.keys(directLiveReadyGuardrails).length > 0;
  const readinessLiveReadyGuardrails = asRecord(pick(v16Readiness, ["live_ready_guardrails"]));
  const liveReadyGuardrailRun = hasDirectLiveReadyGuardrails ? directLiveReadyGuardrails : readinessLiveReadyGuardrails;
  const liveReadyGuardrailFact = readFact(liveReadyGuardrailsQuery.data, "ops.v16-live-ready-guardrails.v2");
  const liveReadyGuardrailRequestFailed = liveReadyGuardrailsQuery.isError || liveReadyGuardrailsQuery.isRefetchError;
  const liveReadyGuardrailKnown = factIsKnown(liveReadyGuardrailFact, liveReadyGuardrailRequestFailed);
  const liveReadyGuardrails = pickArray(liveReadyGuardrailRun, ["items"]);
  const latestGuardrail = asRecord(liveReadyGuardrails[0]);
  const directProposalRegistry = asRecord(pick(proposalRegistryQuery.data, ["proposals"]));
  const hasDirectProposalRegistry = Object.keys(directProposalRegistry).length > 0;
  const readinessProposalRegistry = asRecord(pick(v16Readiness, ["proposal_registry"]));
  const proposalRegistry = hasDirectProposalRegistry ? directProposalRegistry : readinessProposalRegistry;
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
  const liveAutonomyLatestEvent = asRecord(pick(liveAutonomy, ["latest_event"]));
  const liveAutonomyPosture = asRecord(pick(liveAutonomy, ["operational_posture"]));
  const directAutonomousBlueprint = asRecord(pick(readiness, ["autonomous_blueprint"]));
  const v16AutonomousBlueprint = asRecord(pick(v16Readiness, ["autonomous_blueprint"]));
  const autonomousBlueprint = Object.keys(directAutonomousBlueprint).length ? directAutonomousBlueprint : v16AutonomousBlueprint;
  const directAgentAuthority = asRecord(pick(readiness, ["agent_authority"]));
  const v16AgentAuthority = asRecord(pick(v16Readiness, ["agent_authority"]));
  const agentAuthority = Object.keys(directAgentAuthority).length ? directAgentAuthority : v16AgentAuthority;
  const agentScorecard = asRecord(pick(readiness, ["agent_scorecard"]));
  const agentBriefing = asRecord(pick(readiness, ["agent_briefing"]));
  const directAgentChainHealth = asRecord(pick(readiness, ["agent_chain_health"]));
  const v16AgentChainHealth = asRecord(pick(v16Readiness, ["agent_chain_health"]));
  const agentChainHealth = Object.keys(directAgentChainHealth).length ? directAgentChainHealth : v16AgentChainHealth;
  const directProposalContext = asRecord(pick(readiness, ["proposal_generation_context_coverage"]));
  const v16ProposalContext = asRecord(pick(v16Readiness, ["proposal_generation_context_coverage"]));
  const proposalContextCoverage = Object.keys(directProposalContext).length ? directProposalContext : v16ProposalContext;
  const directCandidateContext = asRecord(pick(readiness, ["candidate_generation_context_coverage"]));
  const v16CandidateContext = asRecord(pick(v16Readiness, ["candidate_generation_context_coverage"]));
  const candidateContextCoverage = Object.keys(directCandidateContext).length ? directCandidateContext : v16CandidateContext;
  const directCandidateBridgeReview = asRecord(pick(readiness, ["candidate_bridge_review_coverage"]));
  const v16CandidateBridgeReview = asRecord(pick(v16Readiness, ["candidate_bridge_review_coverage"]));
  const candidateBridgeReviewCoverage = Object.keys(directCandidateBridgeReview).length ? directCandidateBridgeReview : v16CandidateBridgeReview;
  const blueprintBlockers = pickArray(autonomousBlueprint, ["blockers"]);

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
      <div className="dashboard-header">
        <div>
          <div className="eyebrow">自治决策系统</div>
          <h1>自治治理链路</h1>
          <p>多智能体权责、提案总线、候选审查、记忆反馈和执行边界。</p>
        </div>
        <div className="header-status">
          <StatusPill status={displayValue(pickString(autonomousBlueprint, ["status"], ""))} tone={factBoundTone(readinessFact, pickBoolean(autonomousBlueprint, ["ok"], false) ? "ok" : "warn", readinessRequestFailed)} />
          <StatusPill status={displayStage(pickString(v16Readiness, ["phase"], ""))} tone={factBoundTone(readinessFact, "ok", readinessRequestFailed)} />
          <StatusPill status={brainStateKnown ? (readOnly && !affectsTrading ? "交易边界正常" : "边界异常") : "交易边界未知"} tone={factBoundTone(brainStateFact, readOnly && !affectsTrading ? "ok" : "bad", brainStateRequestFailed)} />
          <button className="header-refresh" type="button" disabled={refreshMutation.isPending} onClick={() => refreshMutation.mutate()}>
            <RefreshCw size={15} aria-hidden="true" />
            {refreshMutation.isPending ? "刷新中" : "刷新大脑"}
          </button>
          {refreshMutation.isError ? <span className="error-text small">刷新失败</span> : null}
          {lowImpactMutation.isError ? <span className="error-text small">低影响回放失败</span> : null}
          {mediumImpactMutation.isError ? <span className="error-text small">治理候选失败</span> : null}
          {candidateReviewMutation.isError ? <span className="error-text small">候选审查失败</span> : null}
          {liveReadyEvaluateMutation.isError ? <span className="error-text small">护栏评估失败</span> : null}
          {liveReadyTightenMutation.isError ? <span className="error-text small">收紧失败</span> : null}
        </div>
      </div>

      <div className="page-action-bar" aria-label="自治治理操作">
        <button className="header-refresh" type="button" disabled={lowImpactMutation.isPending} onClick={() => lowImpactMutation.mutate()}><Play size={15} />{lowImpactMutation.isPending ? "运行中" : "低影响回放"}</button>
        <button className="header-refresh" type="button" disabled={mediumImpactMutation.isPending} onClick={() => mediumImpactMutation.mutate()}><ListChecks size={15} />{mediumImpactMutation.isPending ? "生成中" : "生成候选"}</button>
        <button className="header-refresh" type="button" disabled={candidateReviewMutation.isPending} onClick={() => candidateReviewMutation.mutate()}><GitBranch size={15} />{candidateReviewMutation.isPending ? "审查中" : "审查候选"}</button>
        <button className="header-refresh" type="button" disabled={liveReadyEvaluateMutation.isPending} onClick={() => liveReadyEvaluateMutation.mutate()}><ShieldCheck size={15} />{liveReadyEvaluateMutation.isPending ? "评估中" : "护栏评估"}</button>
        <ActionButton icon={ShieldCheck} label="不增风险" variant="ghost" disabled={liveReadyTightenMutation.isPending} confirmMessage="将实盘自治收紧为不允许增加风险。" onAction={() => liveReadyTightenMutation.mutateAsync("no_new_risk")} />
        <ActionButton icon={ShieldCheck} label="仅平仓" variant="danger" disabled={liveReadyTightenMutation.isPending} confirmMessage="将实盘自治收紧为只允许平仓。" onAction={() => liveReadyTightenMutation.mutateAsync("only_close")} />
        <ActionButton icon={ShieldCheck} label="冻结" variant="danger" disabled={liveReadyTightenMutation.isPending} confirmMessage="将冻结实盘自治能力，后续需要重新评估才能放开。" onAction={() => liveReadyTightenMutation.mutateAsync("frozen")} />
      </div>

      <div className="stat-grid v15-stat-grid">
        <StatTile icon={Network} label="大纲对齐" value={displayValue(pickString(autonomousBlueprint, ["status"], ""))} detail={`阻断 ${formatDecimal(blueprintBlockers.length, 0)} / 检查 ${formatDecimal(pickArray(autonomousBlueprint, ["checks"]).length, 0)}`} tone={factBoundTone(readinessFact, pickBoolean(autonomousBlueprint, ["ok"], false) ? "ok" : "warn", readinessRequestFailed)} />
        <StatTile icon={UsersRound} label="Agent 权责" value={formatDecimal(pickNumber(agentAuthority, ["registered_agents"], 0), 0)} detail={`未知 ${formatDecimal(countOf(pick(agentAuthority, ["unknown_sources"])), 0)} / 违规 ${formatDecimal(countOf(pick(agentAuthority, ["contract_violations"])), 0)}`} tone={factBoundTone(readinessFact, pickBoolean(agentAuthority, ["ok"], false) ? "ok" : "warn", readinessRequestFailed)} />
        <StatTile icon={Route} label="上下文覆盖" value={displayValue(pickString(proposalContextCoverage, ["status"], ""))} detail={`候选 ${displayValue(pickString(candidateContextCoverage, ["status"], ""))} / 桥接 ${displayValue(pickString(candidateBridgeReviewCoverage, ["status"], ""))}`} tone={factBoundTone(readinessFact, statusTone(pickString(proposalContextCoverage, ["status"], "")), readinessRequestFailed)} />
        <StatTile icon={BrainCircuit} label="策略姿态" value={displayValue(pickString(worldModel, ["strategy_posture"], ""))} detail={displayValue(pickString(worldModel, ["market_regime"], ""))} tone={factBoundTone(brainStateFact, statTone, brainStateRequestFailed)} />
        <StatTile icon={ShieldCheck} label="审查结论" value={displayValue(criticVerdict)} detail={displayValue(pickString(critic, ["max_allowed_action_scope"], ""))} tone={factBoundTone(brainStateFact, criticVerdict === "pass" ? "ok" : "warn", brainStateRequestFailed)} />
        <StatTile icon={Database} label="记忆命中" value={formatDecimal(memoryItems.length, 0)} detail={`负面 ${formatDecimal(negativeMemory.length, 0)} / 反证 ${formatDecimal(counterEvidence.length, 0)}`} tone={factBoundTone(memoryFact, negativeMemory.length ? "warn" : "ok", memoryRequestFailed)} />
        <StatTile icon={Workflow} label="假设" value={formatDecimal(hypotheses.length, 0)} detail={formatTime(pick(brainState, ["created_at"]))} tone={factBoundTone(brainStateFact, hypotheses.length ? "ok" : "warn", brainStateRequestFailed)} />
        {Object.keys(actionPlanRun).length ? <StatTile icon={ListChecks} label="影子计划" value={formatDecimal(actionPlans.length, 0)} detail={displayValue(pickString(actionPlanRun, ["status"], ""))} tone={factBoundTone(actionPlanFact, actionPlans.length ? "ok" : "warn", actionPlanRequestFailed)} /> : null}
        {Object.keys(actionPlanEvalRun).length ? <StatTile icon={GitBranch} label="后验评价" value={formatDecimal(actionPlanEvals.length, 0)} detail={displayValue(pickString(actionPlanEvalRun, ["status"], ""))} tone={factBoundTone(actionPlanEvalFact, actionPlanEvals.length ? "ok" : "warn", actionPlanEvalRequestFailed)} /> : null}
        {Object.keys(lowImpactExecutionRun).length ? <StatTile icon={Play} label="低影响执行" value={formatDecimal(lowImpactExecutions.length, 0)} detail={displayValue(pickString(lowImpactExecutionRun, ["status"], ""))} tone={factBoundTone(lowImpactFact, lowImpactExecutions.length ? "ok" : "warn", lowImpactRequestFailed)} /> : null}
        {Object.keys(mediumImpactGovernanceRun).length ? <StatTile icon={ListChecks} label="治理候选" value={formatDecimal(mediumImpactGovernance.length, 0)} detail={displayValue(pickString(mediumImpactGovernanceRun, ["status"], ""))} tone={factBoundTone(mediumImpactFact, mediumImpactGovernance.length ? "ok" : "warn", mediumImpactRequestFailed)} /> : null}
        {Object.keys(candidateReviewRun).length ? <StatTile icon={GitBranch} label="候选审查" value={formatDecimal(candidateReviews.length, 0)} detail={displayValue(pickString(candidateReviewRun, ["status"], ""))} tone={factBoundTone(candidateReviewFact, candidateReviews.length ? "ok" : "warn", candidateReviewRequestFailed)} /> : null}
        <StatTile icon={ListChecks} label="提案总线" value={formatDecimal(pickNumber(proposalSummary, ["proposal_count"], proposalItems.length), 0)} detail={`冲突 ${formatDecimal(pickNumber(proposalSummary, ["conflict_count"], 0), 0)}`} tone={factBoundTone(proposalRegistryFact, pickNumber(proposalSummary, ["high_unresolved_conflict_count"], 0) ? "bad" : "ok", proposalRegistryRequestFailed)} />
        {Object.keys(liveAutonomy).length ? <StatTile icon={ShieldCheck} label="实盘自治" value={displayValue(pickString(liveAutonomy, ["autonomy_mode"], ""))} detail={displayValue(pickString(liveAutonomyEvaluation, ["status"], ""))} tone={factBoundTone(liveAutonomyView.fact, pickBoolean(liveAutonomy, ["live_autonomy_unlocked"], false) ? "ok" : liveAutonomyBlockers.length ? "warn" : "mute", liveAutonomyRequestFailed)} /> : null}
        <StatTile icon={ShieldCheck} label="实盘护栏" value={pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "已锁定" : formatDecimal(liveReadyGuardrails.length, 0)} detail={displayValue(pickString(liveReadyGuardrailRun, ["status"], pickString(latestGuardrail, ["action_recommendation.target_mode"], "live-ready")))} tone={factBoundTone(liveReadyGuardrailFact, pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "ok" : "warn", liveReadyGuardrailRequestFailed)} />
      </div>

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
              liveAutonomy={liveAutonomyKnown ? liveAutonomy : {}}
            />
          </div>
        </MetricCard>

        <MetricCard title="Agent 权责合同" className="wide-panel">
          <div className={readinessKnown ? "" : "fact-unverified"}>
            <AgentAuthorityPanel
              agentAuthority={agentAuthority}
              agentScorecard={agentScorecard}
              agentBriefing={agentBriefing}
              chainHealth={agentChainHealth}
            />
          </div>
        </MetricCard>

        <MetricCard title="上下文覆盖" className="wide-panel">
          <div className={readinessKnown ? "" : "fact-unverified"}>
            <CoveragePanel
              proposalContext={proposalContextCoverage}
              candidateContext={candidateContextCoverage}
              candidateReview={candidateBridgeReviewCoverage}
            />
          </div>
        </MetricCard>

        <MetricCard title="世界模型" className="wide-panel">
          <div className={`v15-mini-grid ${brainStateKnown ? "" : "fact-unverified"}`.trim()}>
            <CompactMetric label="市场状态" value={displayValue(pickString(worldModel, ["market_regime"], ""))} tone="mute" />
            <CompactMetric label="因子姿态" value={displayValue(pickString(worldModel, ["factor_posture"], ""))} tone={factBoundTone(brainStateFact, toneFromStatus(pickString(worldModel, ["factor_posture"], "")), brainStateRequestFailed)} />
            <CompactMetric label="执行姿态" value={displayValue(pickString(worldModel, ["execution_posture"], ""))} tone={factBoundTone(brainStateFact, toneFromStatus(pickString(worldModel, ["execution_posture"], "")), brainStateRequestFailed)} />
            <CompactMetric label="学习姿态" value={displayValue(pickString(worldModel, ["learning_posture"], ""))} tone="mute" />
            <CompactMetric label="自治姿态" value={displayValue(pickString(worldModel, ["autonomy_posture"], ""))} tone={factBoundTone(brainStateFact, toneFromStatus(pickString(worldModel, ["autonomy_posture"], "")), brainStateRequestFailed)} />
            <CompactMetric label="事故模式" value={displayValue(pickString(worldModel, ["incident_mode"], "normal"))} tone={factBoundTone(brainStateFact, pickString(worldModel, ["incident_mode"], "normal") === "normal" ? "ok" : "warn", brainStateRequestFailed)} />
          </div>
          <div className="v16-boundary">
            <Field label="只读" value={readOnly ? "true" : "false"} tone={factBoundTone(brainStateFact, boolTone(readOnly), brainStateRequestFailed)} />
            <Field label="影响交易" value={affectsTrading ? "true" : "false"} tone={factBoundTone(brainStateFact, affectsTrading ? "bad" : "ok", brainStateRequestFailed)} />
            <Field label="不执行计划" value={pickBoolean(boundary, ["does_not_execute_action_plan"], false) ? "true" : "false"} tone={factBoundTone(brainStateFact, boolTone(pickBoolean(boundary, ["does_not_execute_action_plan"], false)), brainStateRequestFailed)} />
            <Field label="不写学习样本" value={pickBoolean(boundary, ["does_not_write_learning_samples"], false) ? "true" : "false"} tone={factBoundTone(brainStateFact, boolTone(pickBoolean(boundary, ["does_not_write_learning_samples"], false)), brainStateRequestFailed)} />
          </div>
        </MetricCard>

        <MetricCard title="提案总线" className="wide-panel">
          <div className={`v16-boundary ${proposalRegistryKnown ? "" : "fact-unverified"}`.trim()}>
            <Field label="提案" value={formatDecimal(pickNumber(proposalSummary, ["proposal_count"], proposalItems.length), 0)} />
            <Field label="活跃" value={formatDecimal(pickNumber(proposalSummary, ["active_count"], 0), 0)} />
            <Field label="冲突" value={formatDecimal(pickNumber(proposalSummary, ["conflict_count"], 0), 0)} tone={factBoundTone(proposalRegistryFact, pickNumber(proposalSummary, ["conflict_count"], 0) ? "warn" : "ok", proposalRegistryRequestFailed)} />
            <Field label="高危未解" value={formatDecimal(pickNumber(proposalSummary, ["high_unresolved_conflict_count"], 0), 0)} tone={factBoundTone(proposalRegistryFact, pickNumber(proposalSummary, ["high_unresolved_conflict_count"], 0) ? "bad" : "ok", proposalRegistryRequestFailed)} />
            <Field label="低可信" value={formatDecimal(pickNumber(proposalSummary, ["low_reliability_count"], 0), 0)} tone={factBoundTone(proposalRegistryFact, pickNumber(proposalSummary, ["low_reliability_count"], 0) ? "warn" : "ok", proposalRegistryRequestFailed)} />
            <Field label="证据过期" value={formatDecimal(pickNumber(proposalSummary, ["stale_evidence_count"], 0), 0)} tone={factBoundTone(proposalRegistryFact, pickNumber(proposalSummary, ["stale_evidence_count"], 0) ? "warn" : "ok", proposalRegistryRequestFailed)} />
            <Field label="契约" value={displayContract(pickString(proposalRegistry, ["schema_version"], ""))} />
          </div>
          <div className="brain-card-actions">
            <button className="header-refresh" type="button" disabled={proposalRefreshMutation.isPending} onClick={() => proposalRefreshMutation.mutate()}>
              <RefreshCw size={15} aria-hidden="true" />
              {proposalRefreshMutation.isPending ? "刷新中" : "刷新总线"}
            </button>
            {proposalRefreshMutation.isError ? <span className="error-text small">总线刷新失败</span> : null}
            {proposalReviewMutation.isError ? <span className="error-text small">审查记录失败</span> : null}
          </div>
          <div className={proposalRegistryKnown ? "" : "fact-unverified"}>
            <ProposalRegistryList
              items={proposalItems}
              reviewing={proposalReviewMutation.isPending}
              onReview={(proposalId, route) => proposalReviewMutation.mutate({ proposalId, route })}
            />
          </div>
        </MetricCard>

        <MetricCard title="实盘自治" className="wide-panel">
          <FactBoundary fact={liveAutonomyView.fact} label="实盘自治事实">
            <div className="v16-boundary">
              <Field label="模式" value={displayValue(pickString(liveAutonomy, ["autonomy_mode"], "manual"))} tone={factBoundTone(liveAutonomyView.fact, pickBoolean(liveAutonomy, ["live_autonomy_unlocked"], false) ? "ok" : "warn", liveAutonomyRequestFailed)} />
              <Field label="解锁" value={pickBoolean(liveAutonomy, ["live_autonomy_unlocked"], false) ? "true" : "false"} tone={factBoundTone(liveAutonomyView.fact, boolTone(pickBoolean(liveAutonomy, ["live_autonomy_unlocked"], false)), liveAutonomyRequestFailed)} />
              <Field label="姿态" value={displayValue(pickString(liveAutonomyPosture, ["status"], "locked"))} tone={factBoundTone(liveAutonomyView.fact, pickString(liveAutonomyPosture, ["status"], "locked") === "degraded" ? "bad" : "ok", liveAutonomyRequestFailed)} />
              <Field label="评估" value={displayValue(pickString(liveAutonomyEvaluation, ["status"], "blocked"))} tone={factBoundTone(liveAutonomyView.fact, unlockAllowed ? "ok" : "warn", liveAutonomyRequestFailed)} />
              <Field label="阻断项" value={formatDecimal(liveAutonomyBlockers.length, 0)} tone={factBoundTone(liveAutonomyView.fact, liveAutonomyBlockers.length ? "warn" : "ok", liveAutonomyRequestFailed)} />
              <Field label="建议模式" value={displayValue(pickString(liveAutonomyPosture, ["recommended_incident_mode"], "normal"))} tone={factBoundTone(liveAutonomyView.fact, pickString(liveAutonomyPosture, ["recommended_incident_mode"], "normal") === "normal" ? "ok" : "warn", liveAutonomyRequestFailed)} />
              <Field label="最近事件" value={displayValue(pickString(liveAutonomyLatestEvent, ["status"], "none"))} />
            </div>
          </FactBoundary>
          <div className="brain-card-actions">
            <button className="header-refresh" type="button" disabled={liveUnlockEvaluateMutation.isPending} onClick={() => liveUnlockEvaluateMutation.mutate()}>
              <ShieldCheck size={15} aria-hidden="true" />
              {liveUnlockEvaluateMutation.isPending ? "评估中" : "评估解锁"}
            </button>
            <ActionButton icon={ShieldCheck} label="一次解锁" variant="danger" disabled={liveUnlockMutation.isPending || !unlockAllowed} loading={liveUnlockMutation.isPending} confirmTitle="确认解锁实盘自治" confirmMessage="只有服务端评估通过且事实仍新鲜时才能执行。解锁后自治链路可能影响实盘，请确认护栏、事故模式和阻断项均已检查。" stepUpOnDemand onAction={runLiveUnlock} />
            <ActionButton icon={ShieldCheck} label="撤销自治" variant="danger" disabled={liveRevokeMutation.isPending} loading={liveRevokeMutation.isPending} confirmMessage="撤销后实盘自治立即回到锁定状态。" onAction={() => liveRevokeMutation.mutateAsync()} />
            {liveUnlockEvaluateMutation.isError ? <span className="error-text small">解锁评估失败</span> : null}
            {liveUnlockMutation.isError ? <span className="error-text small">解锁失败</span> : null}
            {liveRevokeMutation.isError ? <span className="error-text small">撤销失败</span> : null}
          </div>
          <div className={`v15-list compact-v15-list ${liveAutonomyKnown ? "" : "fact-unverified"}`.trim()}>
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
                  <StatusPill status={displayValue(status)} tone={factBoundTone(liveAutonomyView.fact, status === "ok" || status === "ready" ? "ok" : "warn", liveAutonomyRequestFailed)} />
                </div>
              );
            })}
          </div>
        </MetricCard>

        <MetricCard title="假设">
          <div className={brainStateKnown ? "" : "fact-unverified"}><HypothesisList items={hypotheses} /></div>
        </MetricCard>

        <MetricCard title="记忆">
          <div className={memoryKnown ? "" : "fact-unverified"}>
            <SectionHead title="负面记忆" status={`${negativeMemory.length}`} tone={factBoundTone(memoryFact, negativeMemory.length ? "warn" : "ok", memoryRequestFailed)} />
            <MemoryList items={negativeMemory} empty="暂无负面记忆命中" />
            <SectionHead title="反证" status={`${counterEvidence.length}`} tone={factBoundTone(memoryFact, counterEvidence.length ? "ok" : "mute", memoryRequestFailed)} />
            <MemoryList items={counterEvidence} empty="暂无反证命中" />
          </div>
        </MetricCard>

        <MetricCard title="影子计划" className="wide-panel">
          <div className={`v16-boundary ${actionPlanKnown ? "" : "fact-unverified"}`.trim()}>
            <Field label="只读计划" value={pickBoolean(actionPlanRun, ["read_only"], true) ? "true" : "false"} tone={factBoundTone(actionPlanFact, boolTone(pickBoolean(actionPlanRun, ["read_only"], true)), actionPlanRequestFailed)} />
            <Field label="影响交易" value={pickBoolean(actionPlanRun, ["affects_trading"], false) ? "true" : "false"} tone={factBoundTone(actionPlanFact, pickBoolean(actionPlanRun, ["affects_trading"], false) ? "bad" : "ok", actionPlanRequestFailed)} />
            <Field label="阶段" value={displayStage(pickString(actionPlanRun, ["phase"], "v16_phase2_shadow_brain"))} />
            <Field label="快照" value={pickString(actionPlanRun, ["snapshot_id"], pickString(brainState, ["snapshot_id"], ""))} />
          </div>
          <div className={actionPlanKnown ? "" : "fact-unverified"}><ActionPlanList items={actionPlans} /></div>
        </MetricCard>

        <MetricCard title="后验评价" className="wide-panel">
          <div className={`v16-boundary ${actionPlanEvalKnown ? "" : "fact-unverified"}`.trim()}>
            <Field label="只读评价" value={pickBoolean(actionPlanEvalRun, ["read_only"], true) ? "true" : "false"} tone={factBoundTone(actionPlanEvalFact, boolTone(pickBoolean(actionPlanEvalRun, ["read_only"], true)), actionPlanEvalRequestFailed)} />
            <Field label="影响交易" value={pickBoolean(actionPlanEvalRun, ["affects_trading"], false) ? "true" : "false"} tone={factBoundTone(actionPlanEvalFact, pickBoolean(actionPlanEvalRun, ["affects_trading"], false) ? "bad" : "ok", actionPlanEvalRequestFailed)} />
            <Field label="评价契约" value={displayContract(pickString(actionPlanEvalRun, ["schema_version"], ""))} />
            <Field label="证据缺口" value={formatDecimal(pickArray(actionPlanEvalRun, ["source_gaps"]).length, 0)} tone={factBoundTone(actionPlanEvalFact, pickArray(actionPlanEvalRun, ["source_gaps"]).length ? "warn" : "ok", actionPlanEvalRequestFailed)} />
          </div>
          <div className={actionPlanEvalKnown ? "" : "fact-unverified"}><EvaluationList items={actionPlanEvals} /></div>
        </MetricCard>

        <MetricCard title="低影响执行" className="wide-panel">
          <div className={`v16-boundary ${lowImpactKnown ? "" : "fact-unverified"}`.trim()}>
            <Field label="白名单" value={pickBoolean(lowImpactExecutionRun, ["boundary.low_impact_only"], true) ? "true" : "false"} tone={factBoundTone(lowImpactFact, boolTone(pickBoolean(lowImpactExecutionRun, ["boundary.low_impact_only"], true)), lowImpactRequestFailed)} />
            <Field label="RiskPolicy" value={pickBoolean(lowImpactExecutionRun, ["boundary.risk_policy_service_required"], true) ? "required" : "missing"} tone={factBoundTone(lowImpactFact, boolTone(pickBoolean(lowImpactExecutionRun, ["boundary.risk_policy_service_required"], true)), lowImpactRequestFailed)} />
            <Field label="执行契约" value={displayContract(pickString(lowImpactExecutionRun, ["schema_version"], ""))} />
            <Field label="最近更新" value={formatTime(pick(lowImpactExecutionRun, ["latest_created_at"]))} />
          </div>
          <div className={lowImpactKnown ? "" : "fact-unverified"}><ExecutionList items={lowImpactExecutions} /></div>
        </MetricCard>

        <MetricCard title="治理候选" className="wide-panel">
          <div className={`v16-boundary ${mediumImpactKnown ? "" : "fact-unverified"}`.trim()}>
            <Field label="只生成候选" value={pickBoolean(mediumImpactGovernanceRun, ["boundary.materializes_governance_candidates_only"], true) ? "true" : "false"} tone={factBoundTone(mediumImpactFact, boolTone(pickBoolean(mediumImpactGovernanceRun, ["boundary.materializes_governance_candidates_only"], true)), mediumImpactRequestFailed)} />
            <Field label="手动桥接" value={pickBoolean(mediumImpactGovernanceRun, ["boundary.policy_suggestion_bridge_manual_only"], true) ? "true" : "false"} tone={factBoundTone(mediumImpactFact, boolTone(pickBoolean(mediumImpactGovernanceRun, ["boundary.policy_suggestion_bridge_manual_only"], true)), mediumImpactRequestFailed)} />
            <Field label="不改权重" value={pickBoolean(mediumImpactGovernanceRun, ["boundary.does_not_apply_factor_weights"], true) ? "true" : "false"} tone={factBoundTone(mediumImpactFact, boolTone(pickBoolean(mediumImpactGovernanceRun, ["boundary.does_not_apply_factor_weights"], true)), mediumImpactRequestFailed)} />
            <Field label="治理契约" value={displayContract(pickString(mediumImpactGovernanceRun, ["schema_version"], ""))} />
            <Field label="最近更新" value={formatTime(pick(mediumImpactGovernanceRun, ["latest_created_at"]))} />
          </div>
          <div className={mediumImpactKnown ? "" : "fact-unverified"}><GovernanceList items={mediumImpactGovernance} /></div>
        </MetricCard>

        <MetricCard title="候选审查" className="wide-panel">
          <div className={`v16-boundary ${candidateReviewKnown ? "" : "fact-unverified"}`.trim()}>
            <Field label="只读审查" value={pickBoolean(candidateReviewRun, ["boundary.review_only"], true) ? "true" : "false"} tone={factBoundTone(candidateReviewFact, boolTone(pickBoolean(candidateReviewRun, ["boundary.review_only"], true)), candidateReviewRequestFailed)} />
            <Field label="桥接预览" value={pickBoolean(candidateReviewRun, ["boundary.bridge_preview_only"], true) ? "true" : "false"} tone={factBoundTone(candidateReviewFact, boolTone(pickBoolean(candidateReviewRun, ["boundary.bridge_preview_only"], true)), candidateReviewRequestFailed)} />
            <Field label="LLM 只建议" value={pickBoolean(candidateReviewRun, ["boundary.llm_advisory_only"], true) ? "true" : "false"} tone={factBoundTone(candidateReviewFact, boolTone(pickBoolean(candidateReviewRun, ["boundary.llm_advisory_only"], true)), candidateReviewRequestFailed)} />
            <Field label="最近更新" value={formatTime(pick(candidateReviewRun, ["latest_created_at"]))} />
          </div>
          <div className={candidateReviewKnown ? "" : "fact-unverified"}><CandidateReviewList items={candidateReviews} /></div>
        </MetricCard>

        <MetricCard title="实盘护栏" className="wide-panel">
          <div className={`v16-boundary ${liveReadyGuardrailKnown ? "" : "fact-unverified"}`.trim()}>
            <Field label="能力锁" value={displayValue(pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "locked" : "blocked")} tone={factBoundTone(liveReadyGuardrailFact, pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "ok" : "warn", liveReadyGuardrailRequestFailed)} />
            <Field label="偏差" value={displayValue(pickString(latestGuardrail, ["broker_local_divergence.status"], pickString(liveReadyGuardrailRun, ["divergence_status"], "")))} tone={factBoundTone(liveReadyGuardrailFact, pickString(latestGuardrail, ["broker_local_divergence.status"], "") === "divergent" ? "bad" : "ok", liveReadyGuardrailRequestFailed)} />
            <Field label="事故" value={displayValue(pickString(latestGuardrail, ["incident_control.mode"], ""))} tone={factBoundTone(liveReadyGuardrailFact, pickString(latestGuardrail, ["incident_control.mode"], "normal") === "normal" ? "ok" : "warn", liveReadyGuardrailRequestFailed)} />
            <Field label="回滚" value={displayValue(pickBoolean(latestGuardrail, ["release_rollback.rollback_ready"], false) ? "ready" : "missing")} tone={factBoundTone(liveReadyGuardrailFact, boolTone(pickBoolean(latestGuardrail, ["release_rollback.rollback_ready"], false)), liveReadyGuardrailRequestFailed)} />
            <Field label="建议模式" value={displayValue(pickString(latestGuardrail, ["action_recommendation.target_mode"], pickString(liveReadyGuardrailRun, ["recommended_mode"], "")))} />
            <Field label="最近更新" value={formatTime(pick(liveReadyGuardrailRun, ["latest_created_at"]))} />
          </div>
          <div className={liveReadyGuardrailKnown ? "" : "fact-unverified"}><GuardrailList items={liveReadyGuardrails} /></div>
        </MetricCard>

        <MetricCard title="Critic 与证据" className="wide-panel">
          <div className="v15-two-col">
            <div>
              <SectionHead title="Critic 异议" status={`${pickArray(critic, ["objections"]).length}`} tone={factBoundTone(brainStateFact, pickArray(critic, ["objections"]).length ? "warn" : "ok", brainStateRequestFailed)} />
              <div className={`v15-list ${brainStateKnown ? "" : "fact-unverified"}`.trim()}>
                {(pickArray(critic, ["objections"]).length ? pickArray(critic, ["objections"]) : ["none"]).map((item, index) => (
                  <div className="v15-list-row" key={`${String(item)}-${index}`}>
                    <div>
                      <strong>{String(item)}</strong>
                      <span>{displayValue(pickString(critic, ["max_allowed_action_scope"], "observe_only"))}</span>
                    </div>
                    <StatusPill status={displayValue(criticVerdict)} tone={factBoundTone(brainStateFact, criticVerdict === "pass" ? "ok" : "warn", brainStateRequestFailed)} />
                  </div>
                ))}
              </div>
            </div>
            <div>
              <SectionHead title="证据缺口" status={`${sourceGaps.length}`} tone={factBoundTone(memoryFact, sourceGaps.length ? "warn" : "ok", memoryRequestFailed)} />
              <div className={memoryKnown ? "" : "fact-unverified"}><MemoryList items={sourceGaps.map((gap) => ({ source_table: "source_gap", source_id: String(gap), text_summary: String(gap), polarity: "neutral" }))} empty="无证据缺口" /></div>
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
          <div className={memoryKnown ? "" : "fact-unverified"}><MemoryList items={memoryItems} empty="暂无索引记忆" /></div>
        </MetricCard>

        <MetricCard title="契约状态">
          <div className="field-list">
            <Field label="后端就绪" value={displayContract(pickString(readiness, ["schema_version"], ""))} />
            <Field label="自治就绪" value={displayContract(pickString(v16Readiness, ["schema_version"], ""))} />
            <Field label="大脑快照" value={pickString(brainState, ["snapshot_id"], "")} />
            <Field label="记忆检索" value={displayContract(pickString(memory, ["schema_version"], ""))} />
            <Field label="影子计划" value={displayContract(pickString(actionPlanRun, ["schema_version"], ""))} />
            <Field label="后验评价" value={displayContract(pickString(actionPlanEvalRun, ["schema_version"], ""))} />
            <Field label="低影响执行" value={displayContract(pickString(lowImpactExecutionRun, ["schema_version"], ""))} />
            <Field label="治理候选" value={displayContract(pickString(mediumImpactGovernanceRun, ["schema_version"], ""))} />
            <Field label="候选审查" value={displayContract(pickString(candidateReviewRun, ["schema_version"], ""))} />
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
