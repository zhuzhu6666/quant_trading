import { lazy, Suspense } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { ArrowRight, BrainCircuit, FileSearch2, HeartPulse, LineChart, Microscope, Network } from "lucide-react";
import { StatusPill } from "@/components/StatusPill";
import { factBoundTone, factIsKnown, factStatusLabel, readFact } from "@/api/fact";
import { useBackendReadinessQuery } from "@/hooks/useCoreQueries";
import { asRecord, pick, pickArray, pickBoolean, pickNumber, pickString } from "@/lib/compat";
import { translateDisplayValue } from "@/lib/display";
import { formatDecimal } from "@/lib/format";

const PnlPage = lazy(() => import("./PnlPage").then((module) => ({ default: module.PnlPage })));
const LearningPage = lazy(() => import("./LearningPage").then((module) => ({ default: module.LearningPage })));
const ModelsPage = lazy(() => import("./ModelsPage").then((module) => ({ default: module.ModelsPage })));
const V16BrainPage = lazy(() => import("./V16BrainPage").then((module) => ({ default: module.V16BrainPage })));
const OpsPage = lazy(() => import("./OpsPage").then((module) => ({ default: module.OpsPage })));
const EvidencePage = lazy(() => import("./EvidencePage").then((module) => ({ default: module.EvidencePage })));

type WorkspaceTab = {
  key: string;
  label: string;
  to: string;
  icon: typeof LineChart;
};

function WorkspaceNav({ label, tabs, active }: { label: string; tabs: WorkspaceTab[]; active: string }) {
  return (
    <nav className="workspace-tabs" aria-label={label}>
      {tabs.map((tab) => {
        const Icon = tab.icon;
        const selected = active === tab.key;
        return (
          <Link className={`workspace-tab ${selected ? "workspace-tab-active" : ""}`} aria-current={selected ? "page" : undefined} key={tab.key} to={tab.to}>
            <Icon size={16} aria-hidden="true" />
            <span>{tab.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}

function WorkspaceContent({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<div className="route-loading" role="status">正在加载…</div>}>{children}</Suspense>;
}

export function PerformanceWorkspace() {
  const { section = "pnl" } = useParams();
  if (section === "risk") return <Navigate to="/trading" replace />;
  if (section !== "pnl") return <Navigate to="/performance/pnl" replace />;
  const tabs = [
    { key: "pnl", label: "收益", to: "/performance/pnl", icon: LineChart },
  ];
  return <div className="workspace"><WorkspaceNav label="收益" tabs={tabs} active={section} /><WorkspaceContent><PnlPage /></WorkspaceContent></div>;
}

type FlowTone = "ok" | "warn" | "bad" | "mute" | "pending" | "stale";

type AutonomyFlowStage = {
  key: string;
  title: string;
  value: string;
  note: string;
  tone: FlowTone;
};

function AutonomyFlowSummary({ active }: { active: string }) {
  const readinessQuery = useBackendReadinessQuery();
  const readiness = asRecord(readinessQuery.data);
  const readinessFact = readFact(readinessQuery.data, "ops.backend-readiness.v2");
  const requestFailed = readinessQuery.isError || readinessQuery.isRefetchError;
  const known = factIsKnown(readinessFact, requestFailed);
  const dimensions = asRecord(pick(readiness, ["readiness_dimensions"]));
  const live = asRecord(pick(readiness, ["live"]));
  const loop = asRecord(pick(live, ["loop"]));
  const autonomyHealth = asRecord(pick(readiness, ["autonomy_health"]));
  const models = asRecord(pick(readiness, ["models"]));
  const modelInfluence = asRecord(pick(models, ["influence"]));
  const governance = asRecord(pick(readiness, ["governance"]));
  const learningWorker = asRecord(pick(readiness, ["learning_worker"]));
  const observationCapability = asRecord(pick(learningWorker, ["observation_capability"]));
  const mutationCapability = asRecord(pick(learningWorker, ["mutation_capability"]));
  const mutationBlockers = asRecord(pick(dimensions, ["blockers"]));
  const autonomousMutationBlockers = pickArray(mutationBlockers, ["autonomous_mutation"]);
  const globalBlockers = pickArray(readiness, ["blockers"]);
  const loopKnown = Object.keys(loop).length > 0;
  const autonomyHealthKnown = Object.keys(autonomyHealth).length > 0;
  const modelsKnown = Object.keys(modelInfluence).length > 0;
  const learningWorkerKnown = Object.keys(learningWorker).length > 0;
  const mutationReadyKnown = pick(dimensions, ["ready_for_autonomous_mutation"]) !== undefined;
  const mutationReady = pickBoolean(dimensions, ["ready_for_autonomous_mutation"], false);
  const loopRunning = pickBoolean(loop, ["running"], false);
  const admissionKnown = pick(loop, ["accepting_new_risk"]) !== undefined;
  const acceptingNewRisk = pickBoolean(loop, ["accepting_new_risk"], true);
  const modelInfluenceEnabled = pickBoolean(modelInfluence, ["demo_enabled"], false);
  const workerMutationAvailable = pickBoolean(mutationCapability, ["available"], false);
  const workerObservationAvailable = pickBoolean(observationCapability, ["available"], false);
  const pendingReviewCount = pickNumber(governance, ["pending_review_count"], 0);
  const agentPosture = pickString(autonomyHealth, ["posture", "status"], "");
  const agentRestricted = ["constrained", "shadow_only", "frozen"].includes(agentPosture);

  const unknownStage = (key: string, title: string): AutonomyFlowStage => ({
    key,
    title,
    value: "待确认",
    note: "事实未确认",
    tone: known ? "pending" : factBoundTone(readinessFact, "warn", requestFailed),
  });

  const stages: AutonomyFlowStage[] = [
    known && loopKnown
      ? {
          key: "runtime",
          title: "运行事实",
          value: loopRunning ? (admissionKnown ? (acceptingNewRisk ? "运行中" : "运行但不接新风险") : "运行状态待确认") : "未运行",
          note: admissionKnown && !acceptingNewRisk ? "Live Loop 当前拒绝新风险" : "实时循环与后端状态",
          tone: !loopRunning ? "bad" : !admissionKnown ? "pending" : acceptingNewRisk ? "ok" : "warn",
        }
      : unknownStage("runtime", "运行事实"),
    known && autonomyHealthKnown
      ? {
          key: "agents",
          title: "智能体分析",
          value: agentPosture ? translateDisplayValue(agentPosture) : "待确认",
          note: `${formatDecimal(pickArray(autonomyHealth, ["blockers"]).length, 0)} 个链路限制`,
          tone: agentPosture ? (agentRestricted || pickArray(autonomyHealth, ["blockers"]).length ? "warn" : "ok") : "pending",
        }
      : unknownStage("agents", "智能体分析"),
    known && modelsKnown
      ? {
          key: "models",
          title: "模型辅助",
          value: modelInfluenceEnabled ? "受控候选" : "只观察",
          note: `${formatDecimal(pickNumber(models, ["model_count"], 0), 0)} 个模型 · 不直接下单`,
          tone: "warn",
        }
      : unknownStage("models", "模型辅助"),
    known && learningWorkerKnown
      ? {
          key: "learning",
          title: "学习候选",
          value: workerMutationAvailable ? "可产出候选" : workerObservationAvailable ? "仅观察" : "学习器待确认",
          note: `待治理 ${formatDecimal(pendingReviewCount, 0)} · ${translateDisplayValue(pickString(learningWorker, ["state", "boot_status"], "状态未知"))}`,
          tone: workerMutationAvailable ? "ok" : workerObservationAvailable ? "warn" : "pending",
        }
      : unknownStage("learning", "学习候选"),
    known && mutationReadyKnown
      ? {
          key: "governance",
          title: "治理写回",
          value: mutationReady ? "允许受控写回" : "写回被阻断",
          note: mutationReady
            ? "仍需按治理边界执行"
            : `${formatDecimal(autonomousMutationBlockers.length, 0)} 个阻断 · 不自动改权重/权限`,
          tone: mutationReady ? "ok" : "warn",
        }
      : unknownStage("governance", "治理写回"),
  ];

  const activePageLabel = active === "chain" ? "运行与裁决" : active === "learning" ? "学习与候选" : active === "models" ? "模型与数据" : "闭环总览";
  const conclusion = !known
    ? "运行事实待确认"
    : globalBlockers.length
      ? "运行事实存在阻断"
      : !mutationReadyKnown
        ? "治理写回状态待确认"
      : !mutationReady
        ? "运行在线，但治理写回受限"
        : "闭环在线，可按边界推进";
  const conclusionNote = !known
    ? "先确认后端事实，再判断学习、模型和治理状态。"
    : globalBlockers.length
      ? `${formatDecimal(globalBlockers.length, 0)} 个运行阻断；自治页面只展示事实，不会绕过它们。`
      : !mutationReadyKnown
        ? "后端事实已接入，但治理授权维度尚未返回，不能把它当作可写回。"
      : !mutationReady
        ? `${formatDecimal(autonomousMutationBlockers.length, 0)} 个治理前置条件未满足；这不等于后端停止。`
        : "模型、学习和治理仍由各自权限边界负责，最终动作不在智能系统页面直接生成。";

  return (
    <section className="autonomy-flow-summary" aria-label="智能系统闭环状态">
      <div className="autonomy-flow-conclusion">
        <div>
          <span className="autonomy-flow-kicker">现在先看这一句</span>
          <strong>{conclusion}</strong>
          <p>{conclusionNote}</p>
        </div>
        <div className="autonomy-flow-meta">
          <StatusPill status={`事实 ${factStatusLabel(readinessFact, requestFailed)}`} tone={factBoundTone(readinessFact, known ? "ok" : "warn", requestFailed)} fact={readinessFact} requestFailed={requestFailed} />
          <span>当前页：{activePageLabel}</span>
        </div>
      </div>
      <ol className="autonomy-flow-stages">
        {stages.map((stage, index) => (
          <li className={`autonomy-flow-stage autonomy-flow-stage-${stage.tone}`} key={stage.key}>
            <span className="autonomy-flow-index">{index + 1}</span>
            <div>
              <span className="autonomy-flow-title">{stage.title}</span>
              <strong>{stage.value}</strong>
              <small>{stage.note}</small>
            </div>
            {index < stages.length - 1 ? <ArrowRight className="autonomy-flow-arrow" size={14} aria-hidden="true" /> : null}
          </li>
        ))}
      </ol>
      <p className="autonomy-flow-reading">读法：运行事实 → 智能体分析 → 模型观察 → 学习形成候选 → 治理决定是否写回；任何一步只显示“待确认”，都不能当作通过。</p>
    </section>
  );
}

export function AutonomyWorkspace() {
  const { section = "chain" } = useParams();
  if (!["chain", "learning", "models"].includes(section)) return <Navigate to="/autonomy/chain" replace />;
  const tabs = [
    { key: "chain", label: "1. 运行与裁决", to: "/autonomy/chain", icon: Network },
    { key: "learning", label: "2. 学习与候选", to: "/autonomy/learning", icon: BrainCircuit },
    { key: "models", label: "3. 模型与数据", to: "/autonomy/models", icon: Microscope },
  ];
  const content = section === "learning" ? <LearningPage embedded /> : section === "models" ? <ModelsPage embedded /> : <V16BrainPage embedded />;
  return (
    <div className="workspace intelligence-workspace">
      <header className="intelligence-header">
        <div>
          <span>智能系统</span>
          <h1>自治交易智能闭环</h1>
          <p>这里看的是“事实如何变成候选、候选如何经过治理”的完整链路；交易执行和硬风控仍由原有运行链负责。</p>
        </div>
        <div className="intelligence-header-note">
          <StatusPill status="只读投影" tone="ok" />
          <span>不在此页直接改交易、风控或模型权限</span>
        </div>
      </header>
      <AutonomyFlowSummary active={section} />
      <WorkspaceNav label="智能系统组成" tabs={tabs} active={section} />
      <WorkspaceContent>{content}</WorkspaceContent>
    </div>
  );
}

export function SystemWorkspace() {
  const { section = "health" } = useParams();
  if (!["health", "evidence"].includes(section)) return <Navigate to="/ops/health" replace />;
  const tabs = [
    { key: "health", label: "系统健康", to: "/ops/health", icon: HeartPulse },
    { key: "evidence", label: "运行证据", to: "/ops/evidence", icon: FileSearch2 },
  ];
  return <div className="workspace"><WorkspaceNav label="系统运维" tabs={tabs} active={section} /><WorkspaceContent>{section === "evidence" ? <EvidencePage /> : <OpsPage />}</WorkspaceContent></div>;
}
