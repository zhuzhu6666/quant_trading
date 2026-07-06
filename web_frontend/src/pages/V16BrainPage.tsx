import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BrainCircuit, Database, GitBranch, ListChecks, Play, RefreshCw, ShieldCheck, Sparkles, Workflow } from "lucide-react";
import {
  evaluateBrainLiveReadyGuardrail,
  getBackendReadiness,
  getBrainActionPlanEvals,
  getBrainActionPlans,
  getBrainLiveReadyGuardrails,
  getBrainLowImpactExecutions,
  getBrainMediumImpactGovernance,
  getBrainMemory,
  getBrainState,
  materializeBrainMediumImpactGovernance,
  runBrainLowImpactExecution,
  tightenBrainLiveReadyGuardrail,
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
        return (
          <div className="v15-list-row" key={`${source}-${sourceId}-${index}`}>
            <div>
              <strong>{pickString(item, ["text_summary"], source)}</strong>
              <span>
                {source} · evidence {formatDecimal(pickNumber(item, ["evidence_score"], 0), 2)} · similarity {formatDecimal(pickNumber(item, ["similarity_score"], 0), 2)}
              </span>
            </div>
            <StatusPill status={polarity} tone={polarity === "negative" ? "bad" : polarity === "positive" ? "ok" : "mute"} />
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
              <StatusPill status={risk} tone={riskTone(risk)} />
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
  if (!items.length) return <div className="empty-state-small">暂无 shadow action plan</div>;
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
          <article className="brain-action-plan" key={`${pickString(item, ["plan_id"], "plan")}-${index}`}>
            <div className="brain-hypothesis-head">
              <div>
                <strong>{pickString(item, ["action_type"], "action_plan")}</strong>
                <span>
                  {pickString(scope, ["scope_type"], "scope")} · {pickString(scope, ["scope_key"], "--")}
                </span>
              </div>
              <StatusPill status={status} tone={status === "shadow_recorded" ? "ok" : "warn"} />
            </div>
            <div className="v15-mini-grid v15-mini-grid-tight">
              <CompactMetric label="Critic" value={verdict} tone={verdict === "pass" ? "ok" : verdict === "reject" ? "bad" : "warn"} />
              <CompactMetric label="风险级别" value={risk} tone={riskTone(risk)} />
              <CompactMetric label="最大影响" value={pickString(item, ["max_impact"], "none_shadow_only")} tone="ok" />
            </div>
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
  if (!items.length) return <div className="empty-state-small">暂无 shadow evaluation</div>;
  return (
    <div className="brain-action-plan-list">
      {items.slice(0, 8).map((raw, index) => {
        const item = asRecord(raw);
        const comparison = asRecord(pick(item, ["comparison"]));
        const presence = asRecord(pick(comparison, ["source_presence"]));
        const verdict = pickString(item, ["comparison_verdict"], "unknown");
        const coverage = scorePct(pickNumber(item, ["coverage_score"], 0));
        return (
          <article className="brain-action-plan" key={`${pickString(item, ["eval_id"], "eval")}-${index}`}>
            <div className="brain-hypothesis-head">
              <div>
                <strong>{pickString(item, ["action_type"], "action_plan_eval")}</strong>
                <span>{pickString(item, ["scope_type"], "scope")} · {pickString(item, ["status"], "needs_evidence")}</span>
              </div>
              <StatusPill status={verdict} tone={verdict === "supportive" ? "ok" : verdict === "caution" ? "warn" : "mute"} />
            </div>
            <div className="v15-mini-grid v15-mini-grid-tight">
              <CompactMetric label="覆盖率" value={`${formatDecimal(coverage, 1)}%`} tone={coverage >= 50 ? "ok" : "warn"} />
              <CompactMetric label="Replay" value={pickBoolean(presence, ["replay_report"], false) ? "yes" : "no"} tone={boolTone(pickBoolean(presence, ["replay_report"], false))} />
              <CompactMetric label="Outcome" value={pickBoolean(presence, ["trade_outcome_review"], false) ? "yes" : "no"} tone={boolTone(pickBoolean(presence, ["trade_outcome_review"], false))} />
              <CompactMetric label="Supervisor" value={pickBoolean(presence, ["position_supervisor_trace"], false) ? "yes" : "no"} tone={boolTone(pickBoolean(presence, ["position_supervisor_trace"], false))} />
            </div>
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
          <article className="brain-action-plan" key={`${pickString(item, ["execution_id"], "execution")}-${index}`}>
            <div className="brain-hypothesis-head">
              <div>
                <strong>{pickString(item, ["execution_action"], "low_impact_action")}</strong>
                <span>{pickString(item, ["action_type"], "action_plan")} · {formatTime(pick(item, ["created_at"]))}</span>
              </div>
              <StatusPill status={status} tone={status.includes("blocked") ? "bad" : status.includes("downgraded") ? "warn" : "ok"} />
            </div>
            <div className="v15-mini-grid v15-mini-grid-tight">
              <CompactMetric label="证据分" value={`${formatDecimal(scorePct(pickNumber(item, ["evidence_score"], 0)), 1)}%`} tone="ok" />
              <CompactMetric label="RiskPolicy" value={pickBoolean(riskVerdict, ["allowed"], false) ? "allow" : "block"} tone={boolTone(pickBoolean(riskVerdict, ["allowed"], false))} />
              <CompactMetric label="Critic" value={pickString(item, ["critic_verdict"], "unknown")} tone={pickString(item, ["critic_verdict"], "") === "reject" ? "bad" : "warn"} />
              <CompactMetric label="后验" value={pickString(item, ["comparison_verdict"], "unknown")} tone={pickString(item, ["comparison_verdict"], "") === "caution" ? "warn" : "ok"} />
            </div>
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
  if (!items.length) return <div className="empty-state-small">暂无 medium-impact governance</div>;
  return (
    <div className="brain-action-plan-list">
      {items.slice(0, 8).map((raw, index) => {
        const item = asRecord(raw);
        const status = pickString(item, ["status"], "unknown");
        const riskVerdict = asRecord(pick(item, ["risk_verdict"]));
        const decisionPolicy = asRecord(pick(item, ["decision_policy"]));
        return (
          <article className="brain-action-plan" key={`${pickString(item, ["governance_id"], "governance")}-${index}`}>
            <div className="brain-hypothesis-head">
              <div>
                <strong>{pickString(item, ["governance_action"], "governance_action")}</strong>
                <span>{pickString(item, ["scope_type"], "scope")} · {pickString(item, ["scope_key"], "--")}</span>
              </div>
              <StatusPill status={status} tone={status === "suggestion_materialized" ? "ok" : status.includes("blocked") ? "warn" : "mute"} />
            </div>
            <div className="v15-mini-grid v15-mini-grid-tight">
              <CompactMetric label="证据分" value={`${formatDecimal(scorePct(pickNumber(item, ["evidence_score"], 0)), 1)}%`} tone="ok" />
              <CompactMetric label="RiskPolicy" value={pickBoolean(riskVerdict, ["allowed"], false) ? "allow" : "block"} tone={boolTone(pickBoolean(riskVerdict, ["allowed"], false))} />
              <CompactMetric label="DecisionPolicy" value={pickBoolean(decisionPolicy, ["required"], false) ? "preview" : "n/a"} tone="mute" />
              <CompactMetric label="建议" value={pickString(item, ["suggestion_id"], "--")} tone={pickString(item, ["suggestion_id"], "") ? "ok" : "mute"} />
            </div>
            <div className="brain-ref-row">
              <span>{pickString(item, ["comparison_verdict"], "unknown")}</span>
              <span>{pickString(riskVerdict, ["reason"], "--")}</span>
              <span>runtime mutation {pickBoolean(item, ["rollback_plan.runtime_mutation"], false) ? "yes" : "no"}</span>
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
          <article className="brain-action-plan" key={`${pickString(item, ["guardrail_id"], "guardrail")}-${index}`}>
            <div className="brain-hypothesis-head">
              <div>
                <strong>{pickString(recommendation, ["action"], "guardrail")}</strong>
                <span>{formatTime(pick(item, ["created_at"]))} · {pickString(recommendation, ["target_mode"], "--")}</span>
              </div>
              <StatusPill status={status} tone={locked ? "ok" : status.includes("attention") ? "warn" : "mute"} />
            </div>
            <div className="v15-mini-grid v15-mini-grid-tight">
              <CompactMetric label="Capability" value={locked ? "locked" : "blocked"} tone={locked ? "ok" : "warn"} />
              <CompactMetric label="Divergence" value={pickString(divergence, ["status"], "unknown")} tone={divergent ? "bad" : "ok"} />
              <CompactMetric label="Incident" value={pickString(incident, ["mode"], "normal")} tone={pickString(incident, ["mode"], "normal") === "normal" ? "ok" : "warn"} />
              <CompactMetric label="Rollback" value={pickBoolean(rollback, ["rollback_ready"], false) ? "ready" : "missing"} tone={boolTone(pickBoolean(rollback, ["rollback_ready"], false))} />
            </div>
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

export function V16BrainPage() {
  const queryClient = useQueryClient();
  const readinessQuery = useQuery({ queryKey: ["v16", "readiness"], queryFn: getBackendReadiness, refetchInterval: 15_000, staleTime: 5_000 });
  const brainStateQuery = useQuery({ queryKey: ["v16", "brain-state"], queryFn: () => getBrainState(false), refetchInterval: 20_000, staleTime: 8_000 });
  const brainMemoryQuery = useQuery({ queryKey: ["v16", "brain-memory"], queryFn: () => getBrainMemory(false, 80), refetchInterval: 30_000, staleTime: 10_000 });
  const brainActionPlansQuery = useQuery({ queryKey: ["v16", "brain-action-plans"], queryFn: () => getBrainActionPlans(false, 80), refetchInterval: 30_000, staleTime: 10_000 });
  const brainActionPlanEvalsQuery = useQuery({ queryKey: ["v16", "brain-action-plan-evals"], queryFn: () => getBrainActionPlanEvals(false, 80), refetchInterval: 30_000, staleTime: 10_000 });
  const lowImpactExecutionsQuery = useQuery({ queryKey: ["v16", "brain-low-impact-executions"], queryFn: () => getBrainLowImpactExecutions(80), refetchInterval: 30_000, staleTime: 10_000 });
  const mediumImpactGovernanceQuery = useQuery({ queryKey: ["v16", "brain-medium-impact-governance"], queryFn: () => getBrainMediumImpactGovernance(80), refetchInterval: 30_000, staleTime: 10_000 });
  const liveReadyGuardrailsQuery = useQuery({ queryKey: ["v16", "brain-live-ready-guardrails"], queryFn: () => getBrainLiveReadyGuardrails(80), refetchInterval: 30_000, staleTime: 10_000 });

  const refreshMutation = useMutation({
    mutationFn: async () => {
      await getBrainState(true);
      await getBrainMemory(true, 80);
      await getBrainActionPlans(true, 80);
      await getBrainActionPlanEvals(true, 80);
      await getBrainLowImpactExecutions(80);
      await getBrainMediumImpactGovernance(80);
      await getBrainLiveReadyGuardrails(80);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["v16"] });
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
  const directLiveReadyGuardrails = asRecord(pick(liveReadyGuardrailsQuery.data, ["live_ready_guardrails"]));
  const readinessLiveReadyGuardrails = asRecord(pick(v16Readiness, ["live_ready_guardrails"]));
  const liveReadyGuardrailRun = Object.keys(directLiveReadyGuardrails).length ? directLiveReadyGuardrails : readinessLiveReadyGuardrails;
  const liveReadyGuardrails = pickArray(liveReadyGuardrailRun, ["items"]);
  const latestGuardrail = asRecord(liveReadyGuardrails[0]);

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
          <div className="eyebrow">V16 Autonomous Brain</div>
          <h1>V16 自治大脑</h1>
          <p>查看世界模型、shadow 计划、posterior 评价、治理建议和实盘前护栏。</p>
        </div>
        <div className="header-status">
          <StatusPill status={pickString(v16Readiness, ["phase"], "phase5_live_ready_guardrails")} tone="ok" />
          <StatusPill status={readOnly && !affectsTrading ? "交易边界正常" : "边界异常"} tone={readOnly && !affectsTrading ? "ok" : "bad"} />
          <button className="header-refresh" type="button" disabled={refreshMutation.isPending} onClick={() => refreshMutation.mutate()}>
            <RefreshCw size={15} aria-hidden="true" />
            {refreshMutation.isPending ? "刷新中" : "刷新大脑"}
          </button>
          <button className="header-refresh" type="button" disabled={lowImpactMutation.isPending} onClick={() => lowImpactMutation.mutate()}>
            <Play size={15} aria-hidden="true" />
            {lowImpactMutation.isPending ? "运行中" : "运行 P3"}
          </button>
          <button className="header-refresh" type="button" disabled={mediumImpactMutation.isPending} onClick={() => mediumImpactMutation.mutate()}>
            <ListChecks size={15} aria-hidden="true" />
            {mediumImpactMutation.isPending ? "生成中" : "运行 P4"}
          </button>
          <button className="header-refresh" type="button" disabled={liveReadyEvaluateMutation.isPending} onClick={() => liveReadyEvaluateMutation.mutate()}>
            <ShieldCheck size={15} aria-hidden="true" />
            {liveReadyEvaluateMutation.isPending ? "评估中" : "评估 P5"}
          </button>
          <button className="header-refresh" type="button" disabled={liveReadyTightenMutation.isPending} onClick={() => liveReadyTightenMutation.mutate("no_new_risk")}>
            <ShieldCheck size={15} aria-hidden="true" />
            no_new_risk
          </button>
          <button className="header-refresh" type="button" disabled={liveReadyTightenMutation.isPending} onClick={() => liveReadyTightenMutation.mutate("only_close")}>
            <ShieldCheck size={15} aria-hidden="true" />
            only_close
          </button>
          <button className="header-refresh" type="button" disabled={liveReadyTightenMutation.isPending} onClick={() => liveReadyTightenMutation.mutate("frozen")}>
            <ShieldCheck size={15} aria-hidden="true" />
            freeze
          </button>
          {refreshMutation.isError ? <span className="error-text small">刷新失败</span> : null}
          {lowImpactMutation.isError ? <span className="error-text small">P3 失败</span> : null}
          {mediumImpactMutation.isError ? <span className="error-text small">P4 失败</span> : null}
          {liveReadyEvaluateMutation.isError ? <span className="error-text small">P5 失败</span> : null}
          {liveReadyTightenMutation.isError ? <span className="error-text small">收紧失败</span> : null}
        </div>
      </div>

      <div className="stat-grid v15-stat-grid">
        <StatTile icon={BrainCircuit} label="策略姿态" value={pickString(worldModel, ["strategy_posture"], "unknown")} detail={pickString(worldModel, ["market_regime"], "unknown")} tone={statTone} />
        <StatTile icon={ShieldCheck} label="Critic" value={criticVerdict} detail={pickString(critic, ["max_allowed_action_scope"], "observe_only")} tone={criticVerdict === "pass" ? "ok" : "warn"} />
        <StatTile icon={Database} label="记忆命中" value={formatDecimal(memoryItems.length, 0)} detail={`负面 ${formatDecimal(negativeMemory.length, 0)} / 反证 ${formatDecimal(counterEvidence.length, 0)}`} tone={negativeMemory.length ? "warn" : "ok"} />
        <StatTile icon={Workflow} label="假设" value={formatDecimal(hypotheses.length, 0)} detail={formatTime(pick(brainState, ["created_at"]))} tone={hypotheses.length ? "ok" : "warn"} />
        <StatTile icon={ListChecks} label="Action Plans" value={formatDecimal(actionPlans.length, 0)} detail={pickString(actionPlanRun, ["status"], "shadow")} tone={actionPlans.length ? "ok" : "warn"} />
        <StatTile icon={GitBranch} label="Evaluations" value={formatDecimal(actionPlanEvals.length, 0)} detail={pickString(actionPlanEvalRun, ["status"], "posterior")} tone={actionPlanEvals.length ? "ok" : "warn"} />
        <StatTile icon={Play} label="P3 Runs" value={formatDecimal(lowImpactExecutions.length, 0)} detail={pickString(lowImpactExecutionRun, ["status"], "low-impact")} tone={lowImpactExecutions.length ? "ok" : "warn"} />
        <StatTile icon={ListChecks} label="P4 Governance" value={formatDecimal(mediumImpactGovernance.length, 0)} detail={pickString(mediumImpactGovernanceRun, ["status"], "medium-impact")} tone={mediumImpactGovernance.length ? "ok" : "warn"} />
        <StatTile icon={ShieldCheck} label="P5 Guardrails" value={pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "locked" : formatDecimal(liveReadyGuardrails.length, 0)} detail={pickString(liveReadyGuardrailRun, ["status"], pickString(latestGuardrail, ["action_recommendation.target_mode"], "live-ready"))} tone={pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "ok" : "warn"} />
      </div>

      <div className="dashboard-grid v16-grid">
        <MetricCard title="世界模型" className="wide-panel">
          <div className="v15-mini-grid">
            <CompactMetric label="市场状态" value={pickString(worldModel, ["market_regime"], "unknown")} tone="mute" />
            <CompactMetric label="因子姿态" value={pickString(worldModel, ["factor_posture"], "unknown")} tone={toneFromStatus(pickString(worldModel, ["factor_posture"], "unknown"))} />
            <CompactMetric label="执行姿态" value={pickString(worldModel, ["execution_posture"], "unknown")} tone={toneFromStatus(pickString(worldModel, ["execution_posture"], "unknown"))} />
            <CompactMetric label="学习姿态" value={pickString(worldModel, ["learning_posture"], "unknown")} tone="mute" />
            <CompactMetric label="自治姿态" value={pickString(worldModel, ["autonomy_posture"], "unknown")} tone={toneFromStatus(pickString(worldModel, ["autonomy_posture"], "unknown"))} />
            <CompactMetric label="事故模式" value={pickString(worldModel, ["incident_mode"], "normal")} tone={pickString(worldModel, ["incident_mode"], "normal") === "normal" ? "ok" : "warn"} />
          </div>
          <div className="v16-boundary">
            <Field label="只读" value={readOnly ? "true" : "false"} tone={boolTone(readOnly)} />
            <Field label="影响交易" value={affectsTrading ? "true" : "false"} tone={affectsTrading ? "bad" : "ok"} />
            <Field label="不执行计划" value={pickBoolean(boundary, ["does_not_execute_action_plan"], false) ? "true" : "false"} tone={boolTone(pickBoolean(boundary, ["does_not_execute_action_plan"], false))} />
            <Field label="不写学习样本" value={pickBoolean(boundary, ["does_not_write_learning_samples"], false) ? "true" : "false"} tone={boolTone(pickBoolean(boundary, ["does_not_write_learning_samples"], false))} />
          </div>
        </MetricCard>

        <MetricCard title="Hypotheses">
          <HypothesisList items={hypotheses} />
        </MetricCard>

        <MetricCard title="Memory">
          <SectionHead title="Negative memory" status={`${negativeMemory.length}`} tone={negativeMemory.length ? "warn" : "ok"} />
          <MemoryList items={negativeMemory} empty="暂无负面记忆命中" />
          <SectionHead title="Counter evidence" status={`${counterEvidence.length}`} tone={counterEvidence.length ? "ok" : "mute"} />
          <MemoryList items={counterEvidence} empty="暂无反证命中" />
        </MetricCard>

        <MetricCard title="Shadow Action Plans" className="wide-panel">
          <div className="v16-boundary">
            <Field label="只读计划" value={pickBoolean(actionPlanRun, ["read_only"], true) ? "true" : "false"} tone={boolTone(pickBoolean(actionPlanRun, ["read_only"], true))} />
            <Field label="影响交易" value={pickBoolean(actionPlanRun, ["affects_trading"], false) ? "true" : "false"} tone={pickBoolean(actionPlanRun, ["affects_trading"], false) ? "bad" : "ok"} />
            <Field label="Phase" value={pickString(actionPlanRun, ["phase"], "v16_phase2_shadow_brain")} />
            <Field label="Snapshot" value={pickString(actionPlanRun, ["snapshot_id"], pickString(brainState, ["snapshot_id"], "--"))} />
          </div>
          <ActionPlanList items={actionPlans} />
        </MetricCard>

        <MetricCard title="Shadow Evaluations" className="wide-panel">
          <div className="v16-boundary">
            <Field label="只读评价" value={pickBoolean(actionPlanEvalRun, ["read_only"], true) ? "true" : "false"} tone={boolTone(pickBoolean(actionPlanEvalRun, ["read_only"], true))} />
            <Field label="影响交易" value={pickBoolean(actionPlanEvalRun, ["affects_trading"], false) ? "true" : "false"} tone={pickBoolean(actionPlanEvalRun, ["affects_trading"], false) ? "bad" : "ok"} />
            <Field label="Eval schema" value={pickString(actionPlanEvalRun, ["schema_version"], "--")} />
            <Field label="Source gaps" value={formatDecimal(pickArray(actionPlanEvalRun, ["source_gaps"]).length, 0)} tone={pickArray(actionPlanEvalRun, ["source_gaps"]).length ? "warn" : "ok"} />
          </div>
          <EvaluationList items={actionPlanEvals} />
        </MetricCard>

        <MetricCard title="Low-Impact Executions" className="wide-panel">
          <div className="v16-boundary">
            <Field label="白名单" value={pickBoolean(lowImpactExecutionRun, ["boundary.low_impact_only"], true) ? "true" : "false"} tone={boolTone(pickBoolean(lowImpactExecutionRun, ["boundary.low_impact_only"], true))} />
            <Field label="RiskPolicy" value={pickBoolean(lowImpactExecutionRun, ["boundary.risk_policy_service_required"], true) ? "required" : "missing"} tone={boolTone(pickBoolean(lowImpactExecutionRun, ["boundary.risk_policy_service_required"], true))} />
            <Field label="Schema" value={pickString(lowImpactExecutionRun, ["schema_version"], "--")} />
            <Field label="Latest" value={formatTime(pick(lowImpactExecutionRun, ["latest_created_at"]))} />
          </div>
          <ExecutionList items={lowImpactExecutions} />
        </MetricCard>

        <MetricCard title="Medium-Impact Governance" className="wide-panel">
          <div className="v16-boundary">
            <Field label="只生成建议" value={pickBoolean(mediumImpactGovernanceRun, ["boundary.materializes_policy_suggestions_only"], true) ? "true" : "false"} tone={boolTone(pickBoolean(mediumImpactGovernanceRun, ["boundary.materializes_policy_suggestions_only"], true))} />
            <Field label="不改权重" value={pickBoolean(mediumImpactGovernanceRun, ["boundary.does_not_apply_factor_weights"], true) ? "true" : "false"} tone={boolTone(pickBoolean(mediumImpactGovernanceRun, ["boundary.does_not_apply_factor_weights"], true))} />
            <Field label="Schema" value={pickString(mediumImpactGovernanceRun, ["schema_version"], "--")} />
            <Field label="Latest" value={formatTime(pick(mediumImpactGovernanceRun, ["latest_created_at"]))} />
          </div>
          <GovernanceList items={mediumImpactGovernance} />
        </MetricCard>

        <MetricCard title="Live-Ready Guardrails" className="wide-panel">
          <div className="v16-boundary">
            <Field label="Capability lock" value={pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "locked" : "blocked"} tone={pickBoolean(latestGuardrail, ["live_capability_lock.locked"], false) ? "ok" : "warn"} />
            <Field label="Divergence" value={pickString(latestGuardrail, ["broker_local_divergence.status"], pickString(liveReadyGuardrailRun, ["divergence_status"], "--"))} tone={pickString(latestGuardrail, ["broker_local_divergence.status"], "") === "divergent" ? "bad" : "ok"} />
            <Field label="Incident" value={pickString(latestGuardrail, ["incident_control.mode"], "--")} tone={pickString(latestGuardrail, ["incident_control.mode"], "normal") === "normal" ? "ok" : "warn"} />
            <Field label="Rollback" value={pickBoolean(latestGuardrail, ["release_rollback.rollback_ready"], false) ? "ready" : "missing"} tone={boolTone(pickBoolean(latestGuardrail, ["release_rollback.rollback_ready"], false))} />
            <Field label="建议模式" value={pickString(latestGuardrail, ["action_recommendation.target_mode"], pickString(liveReadyGuardrailRun, ["recommended_mode"], "--"))} />
            <Field label="Latest" value={formatTime(pick(liveReadyGuardrailRun, ["latest_created_at"]))} />
          </div>
          <GuardrailList items={liveReadyGuardrails} />
        </MetricCard>

        <MetricCard title="Critic 与证据" className="wide-panel">
          <div className="v15-two-col">
            <div>
              <SectionHead title="Critic objections" status={`${pickArray(critic, ["objections"]).length}`} tone={pickArray(critic, ["objections"]).length ? "warn" : "ok"} />
              <div className="v15-list">
                {(pickArray(critic, ["objections"]).length ? pickArray(critic, ["objections"]) : ["none"]).map((item, index) => (
                  <div className="v15-list-row" key={`${String(item)}-${index}`}>
                    <div>
                      <strong>{String(item)}</strong>
                      <span>{pickString(critic, ["max_allowed_action_scope"], "observe_only")}</span>
                    </div>
                    <StatusPill status={criticVerdict} tone={criticVerdict === "pass" ? "ok" : "warn"} />
                  </div>
                ))}
              </div>
            </div>
            <div>
              <SectionHead title="Source gaps" status={`${sourceGaps.length}`} tone={sourceGaps.length ? "warn" : "ok"} />
              <MemoryList items={sourceGaps.map((gap) => ({ source_table: "source_gap", source_id: String(gap), text_summary: String(gap), polarity: "neutral" }))} empty="无 source gap" />
            </div>
          </div>
          <div className="brain-json-grid">
            <div>
              <SectionHead title="Evidence refs" />
              <JsonBlock value={evidenceRefs} />
            </div>
            <div>
              <SectionHead title="Memory query" />
              <JsonBlock value={{ query_terms: pickArray(memory, ["query_terms"]), source_gaps: sourceGaps }} />
            </div>
          </div>
        </MetricCard>

        <MetricCard title="最近 Memory Index" className="wide-panel">
          <MemoryList items={memoryItems} empty="暂无 indexed memory" />
        </MetricCard>

        <MetricCard title="Readiness Contract">
          <div className="field-list">
            <Field label="Backend readiness" value={pickString(readiness, ["schema_version"], "--")} />
            <Field label="V16 readiness" value={pickString(v16Readiness, ["schema_version"], "--")} />
            <Field label="Brain snapshot" value={pickString(brainState, ["snapshot_id"], "--")} />
            <Field label="Memory schema" value={pickString(memory, ["schema_version"], "--")} />
            <Field label="Action plan schema" value={pickString(actionPlanRun, ["schema_version"], "--")} />
            <Field label="Action eval schema" value={pickString(actionPlanEvalRun, ["schema_version"], "--")} />
            <Field label="P3 schema" value={pickString(lowImpactExecutionRun, ["schema_version"], "--")} />
            <Field label="P4 schema" value={pickString(mediumImpactGovernanceRun, ["schema_version"], "--")} />
          </div>
          <div className="brain-ref-row">
            <GitBranch size={15} aria-hidden="true" />
            <span>{pickString(brainState, ["source"], "backend")}</span>
            <Sparkles size={15} aria-hidden="true" />
            <span>{pickString(brainState, ["phase"], "v16_phase1_read_only_brain")}</span>
          </div>
        </MetricCard>
      </div>
    </section>
  );
}
