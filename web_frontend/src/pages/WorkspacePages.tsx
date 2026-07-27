import { lazy, Suspense } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { ArrowRight, BrainCircuit, FileSearch2, HeartPulse, LineChart, Microscope, Network, ShieldAlert } from "lucide-react";

const PnlPage = lazy(() => import("./PnlPage").then((module) => ({ default: module.PnlPage })));
const RiskPage = lazy(() => import("./RiskPage").then((module) => ({ default: module.RiskPage })));
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
  if (!["pnl", "risk"].includes(section)) return <Navigate to="/performance/pnl" replace />;
  const tabs = [
    { key: "pnl", label: "收益", to: "/performance/pnl", icon: LineChart },
    { key: "risk", label: "风控", to: "/performance/risk", icon: ShieldAlert },
  ];
  return <div className="workspace"><WorkspaceNav label="收益与风控" tabs={tabs} active={section} /><WorkspaceContent>{section === "risk" ? <RiskPage /> : <PnlPage />}</WorkspaceContent></div>;
}

export function AutonomyWorkspace() {
  const { section = "chain" } = useParams();
  if (!["chain", "learning", "models"].includes(section)) return <Navigate to="/autonomy/chain" replace />;
  const tabs = [
    { key: "chain", label: "1. 自治运行", to: "/autonomy/chain", icon: Network },
    { key: "learning", label: "2. 学习治理", to: "/autonomy/learning", icon: BrainCircuit },
    { key: "models", label: "3. 模型能力", to: "/autonomy/models", icon: Microscope },
  ];
  const content = section === "learning" ? <LearningPage embedded /> : section === "models" ? <ModelsPage embedded /> : <V16BrainPage embedded />;
  return (
    <div className="workspace intelligence-workspace">
      <header className="intelligence-header">
        <div>
          <span>智能系统</span>
          <h1>自治交易智能闭环</h1>
          <p>智能体读取运行事实，模型提供观察结果，学习系统形成候选，治理检查通过后才允许受控写回。</p>
        </div>
        <div className="intelligence-loop" aria-label="智能系统运行顺序">
          <span>运行事实</span><ArrowRight size={14} />
          <span>智能体分析</span><ArrowRight size={14} />
          <span>模型辅助</span><ArrowRight size={14} />
          <span>学习候选</span><ArrowRight size={14} />
          <span>治理写回</span><ArrowRight size={14} />
          <span>效果反馈</span>
        </div>
      </header>
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
