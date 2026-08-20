import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { Activity, BookOpen, BrainCircuit, ChevronLeft, ChevronRight, Command, Gauge, GitBranch, Home, LogOut, Menu, Settings2, Shield, X } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { useLiveState } from "@/hooks/useLiveState";
import { useLayoutPreference } from "@/shell/layout";
import { buildCommands } from "@/shell/commands";
import { CommandPalette } from "@/shell/CommandPalette";
import { SafetyRail } from "@/shell/SafetyRail";
import { ServerStatusCard } from "@/shell/ServerStatusCard";
import { getReadinessView } from "@/api/workbench";
import { formatObservedTime } from "@/api/time";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { WorkspaceId } from "@/types/contracts";
import { uiStatus, workspaceLabels } from "@/i18n/zh-CN";

const navigation: { id: WorkspaceId; path: string; label: string; english: string; hint: string; icon: typeof Activity }[] = [
  { id: "trade-ops", path: "/trade-ops", ...workspaceLabels["trade-ops"], icon: Activity },
  { id: "risk-desk", path: "/risk-desk", ...workspaceLabels["risk-desk"], icon: Shield },
  { id: "research", path: "/research", ...workspaceLabels.research, icon: BrainCircuit },
  { id: "governance", path: "/governance", ...workspaceLabels.governance, icon: Gauge },
  { id: "ops", path: "/ops", ...workspaceLabels.ops, icon: Settings2 },
  { id: "workflow", path: "/workflow", ...workspaceLabels.workflow, icon: GitBranch },
];

const visualNavigation = [
  { path: "/trade-ops", label: "概览", hint: "账户与持仓", icon: Home },
  { path: "/research", label: "研究", hint: "行情与证据", icon: BookOpen },
  { path: "/risk-desk", label: "风控", hint: "风险与裁决", icon: Shield },
  { path: "/governance", label: "治理", hint: "审查与发布", icon: Gauge },
  { path: "/ops", label: "运维", hint: "日志与健康", icon: Settings2 },
] as const;

function workspaceFromPath(pathname: string): WorkspaceId {
  const item = navigation.find((entry) => pathname === entry.path || pathname.startsWith(`${entry.path}/`));
  return item?.id ?? "trade-ops";
}

export function WorkbenchShell() {
  const shellRef = useRef<HTMLDivElement>(null);
  const location = useLocation();
  const navigate = useNavigate();
  const { logout, user } = useAuth();
  const live = useLiveState();
  const queryClient = useQueryClient();
  const workspace = workspaceFromPath(location.pathname);
  const { layout, updateLayout } = useLayoutPreference(workspace);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const readiness = useQuery({ queryKey: ["workbench", "readiness"], queryFn: getReadinessView, staleTime: 15_000, retry: false });

  const refreshFacts = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["workbench"] });
  }, [queryClient]);

  const stop = useCallback(async () => {
    navigate("/trade-ops?action=stop");
  }, [navigate]);
  const emergency = useCallback(async () => {
    navigate("/trade-ops?action=emergency");
  }, [navigate]);
  const commands = useMemo(() => buildCommands({ workspace, live, readiness: readiness.data ?? null, navigate, refreshFacts, onStop: stop, onEmergency: emergency }), [workspace, live, readiness.data, navigate, refreshFacts, stop, emergency]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const modifier = event.ctrlKey || event.metaKey;
      if (modifier && event.key.toLowerCase() === "k") { event.preventDefault(); setPaletteOpen(true); }
      if (modifier && event.shiftKey && event.key.toLowerCase() === "l") { event.preventDefault(); document.querySelector<HTMLElement>(".safety-rail")?.focus(); }
      if (modifier && event.shiftKey && event.key.toLowerCase() === "r") { event.preventDefault(); refreshFacts(); }
      const number = Number(event.key);
      if (modifier && number >= 1 && number <= navigation.length) { event.preventDefault(); navigate(navigation[number - 1].path); }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [navigate, refreshFacts]);

  useLayoutEffect(() => {
    const shell = shellRef.current;
    const rail = shell?.querySelector<HTMLElement>(".safety-rail");
    if (!shell || !rail) return;

    const syncRailHeight = () => {
      shell.style.setProperty("--safety-rail-height", `${Math.ceil(rail.getBoundingClientRect().height)}px`);
    };
    syncRailHeight();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(syncRailHeight);
    observer.observe(rail);
    return () => observer.disconnect();
  }, []);

  return <div ref={shellRef} className={`workbench cockpit-theme reference-theme ${layout.sidebar_collapsed ? "workbench-sidebar-collapsed" : ""}`}>
    <SafetyRail onRefresh={refreshFacts} />
    <a className="skip-link" href="#workbench-main">跳到主要内容</a>
    <aside className={`workbench-sidebar ${mobileNavOpen ? "workbench-sidebar-open" : ""}`} aria-label="工作区导航">
      <div className="sidebar-head"><Link to="/trade-ops" className="sidebar-title"><span className="sidebar-logo">Q</span><span><strong>WORKBENCH</strong><small>视觉预览</small></span></Link><button type="button" className="mobile-close" onClick={() => setMobileNavOpen(false)} aria-label="关闭工作区导航"><X size={16} /></button></div>
      <div className="workspace-nav reference-workspace-nav"><span className="nav-kicker">导航</span>{visualNavigation.map((item, index) => { const Icon = item.icon; const itemUrl = new URL(item.path, window.location.origin); const itemView = new URLSearchParams(itemUrl.search).get("view"); const active = location.pathname === itemUrl.pathname && (!itemView || new URLSearchParams(location.search).get("view") === itemView); return <NavLink key={item.path} to={item.path} onClick={() => setMobileNavOpen(false)} className={`workspace-link ${active ? "workspace-link-active" : ""}`}><span className="workspace-index">0{index + 1}</span><Icon size={20} aria-hidden="true" /><span className="workspace-copy"><strong>{item.label}</strong><small>{item.hint}</small></span></NavLink>; })}</div>
      <div className="reference-sidebar-status"><ServerStatusCard /></div>
      <div className="sidebar-utility"><button type="button" onClick={() => setPaletteOpen(true)}><Command size={15} />命令面板 <kbd>⌘K</kbd></button><button type="button" onClick={() => updateLayout({ sidebar_collapsed: false })}><Settings2 size={15} />重置侧栏</button></div>
      <div className="sidebar-bottom reference-sidebar-bottom"><div className="sidebar-garden" aria-hidden="true"><span className="plant-leaf plant-leaf-a" /><span className="plant-leaf plant-leaf-b" /><span className="plant-leaf plant-leaf-c" /><span className="plant-leaf plant-leaf-d" /><span className="plant-stem" /><span className="plant-pot" /><span className="plant-plinth" /></div><div className="sidebar-user"><div><strong>{user ?? "操作员"}</strong><small>视觉预览</small></div><button type="button" onClick={logout} aria-label="退出登录" title="退出登录"><LogOut size={15} /></button></div></div>
      <button type="button" className="sidebar-collapse" onClick={() => updateLayout({ sidebar_collapsed: !layout.sidebar_collapsed })} aria-label={layout.sidebar_collapsed ? "展开导航" : "折叠导航"}>{layout.sidebar_collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}</button>
    </aside>
    <button className={`mobile-nav-scrim ${mobileNavOpen ? "mobile-nav-scrim-open" : ""}`} type="button" onClick={() => setMobileNavOpen(false)} aria-label="关闭导航" />
    <main id="workbench-main" className="workbench-main" tabIndex={-1}>
      <div className="workbench-mobile-head"><button type="button" onClick={() => setMobileNavOpen(true)} aria-label="打开导航"><Menu size={18} /></button><strong>{navigation.find((item) => item.id === workspace)?.label}</strong><button type="button" onClick={() => setPaletteOpen(true)} aria-label="打开命令面板"><Command size={17} /></button></div>
      <div className="workbench-context-line"><span>工作区 / {workspaceLabels[workspace].label}</span><span>模式 / 仅服务端事实</span><span>布局 / v{layout.layout_version}</span></div>
      <div className="workbench-stage">
        <div className="workbench-primary"><Outlet /></div>
      </div>
      <footer className="workbench-footer reference-footer"><span className={`footer-connection footer-${live.connection}`}><i />数据连接</span><span>{uiStatus(live.connection)}</span><span>事实观测　{live.lastCompleteSnapshotAt ? formatObservedTime(live.lastCompleteSnapshotAt) : "未知"}</span><span>经纪商　{live.snapshot?.broker ?? "未知"}</span><span className="footer-spacer" /><span>审计活动　经后端路由</span><span className="footer-warning">⚠　本界面仅供参考，不构成投资建议</span></footer>
    </main>
    <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} commands={commands} />
  </div>;
}

export { navigation, workspaceFromPath };
