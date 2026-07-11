import { lazy, Suspense } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { BrainCircuit, LineChart, Microscope, Network, ShieldAlert } from "lucide-react";

const PnlPage = lazy(() => import("./PnlPage").then((module) => ({ default: module.PnlPage })));
const RiskPage = lazy(() => import("./RiskPage").then((module) => ({ default: module.RiskPage })));
const LearningPage = lazy(() => import("./LearningPage").then((module) => ({ default: module.LearningPage })));
const ModelsPage = lazy(() => import("./ModelsPage").then((module) => ({ default: module.ModelsPage })));
const V15CockpitPage = lazy(() => import("./V15CockpitPage").then((module) => ({ default: module.V15CockpitPage })));
const V16BrainPage = lazy(() => import("./V16BrainPage").then((module) => ({ default: module.V16BrainPage })));

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

export function GovernanceWorkspace() {
  const { section = "learning" } = useParams();
  if (!["learning", "models"].includes(section)) return <Navigate to="/governance/learning" replace />;
  const tabs = [
    { key: "learning", label: "学习治理", to: "/governance/learning", icon: BrainCircuit },
    { key: "models", label: "模型观察", to: "/governance/models", icon: Microscope },
  ];
  return <div className="workspace"><WorkspaceNav label="学习与模型" tabs={tabs} active={section} /><WorkspaceContent>{section === "models" ? <ModelsPage /> : <LearningPage />}</WorkspaceContent></div>;
}

export function AutonomyWorkspace() {
  const { section = "runtime" } = useParams();
  if (!["runtime", "chain"].includes(section)) return <Navigate to="/autonomy/runtime" replace />;
  const tabs = [
    { key: "runtime", label: "运行治理", to: "/autonomy/runtime", icon: Network },
    { key: "chain", label: "自治链路", to: "/autonomy/chain", icon: BrainCircuit },
  ];
  return <div className="workspace"><WorkspaceNav label="自治中枢" tabs={tabs} active={section} /><WorkspaceContent>{section === "chain" ? <V16BrainPage /> : <V15CockpitPage />}</WorkspaceContent></div>;
}
