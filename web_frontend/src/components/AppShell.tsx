import { useEffect, useRef, useState } from "react";
import { Link, Navigate, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { LogOut, Network, Activity, ShieldAlert, Settings2, BrainCircuit, Cpu, Menu, X } from "lucide-react";
import { getSystemLoad } from "@/api/domains/system";
import { useAuth } from "@/contexts/AuthContext";
import { formatDecimal } from "@/lib/format";
import { queryKeys } from "@/api/queryKeys";

const navGroups = [
  { label: "交易", items: [
    { to: "/overview", match: "/overview", label: "运行地图", icon: Network },
    { to: "/trading", match: "/trading", label: "交易", icon: Activity },
    { to: "/performance/pnl", match: "/performance", label: "收益风控", icon: ShieldAlert },
  ] },
  { label: "治理", items: [
    { to: "/autonomy/chain", match: "/autonomy", label: "智能系统", icon: BrainCircuit },
  ] },
  { label: "系统", items: [
    { to: "/ops/health", match: "/ops", label: "系统运维", icon: Settings2 },
  ] },
];
const navItems = navGroups.flatMap((group) => group.items);
const legacyPaths = ["/pnl", "/risk", "/learning", "/models", "/v15", "/v16"];

function loadTone(value: number): "ok" | "warn" | "bad" {
  if (value >= 85) return "bad";
  if (value >= 70) return "warn";
  return "ok";
}

function NavLoadItem({ label, value }: { label: string; value: number }) {
  const safeValue = Math.min(Math.max(value, 0), 100);
  return (
    <div className={`nav-load-item nav-load-${loadTone(safeValue)}`}>
      <span>{label}</span>
      <strong>{formatDecimal(safeValue, 0)}%</strong>
      <i aria-hidden="true">
        <b style={{ width: `${safeValue}%` }} />
      </i>
    </div>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const mainRef = useRef<HTMLElement>(null);
  const { logout, user } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const systemLoadQuery = useQuery({
    queryKey: queryKeys.systemLoad,
    queryFn: getSystemLoad,
    refetchInterval: 5_000,
    staleTime: 2_500,
    retry: false,
    enabled: location.pathname !== "/login",
  });

  const active = location.pathname;
  useEffect(() => {
    mainRef.current?.focus({ preventScroll: true });
    setMenuOpen(false);
  }, [active]);
  if (active === "/login") {
    return <>{children}</>;
  }

  if (!legacyPaths.includes(active) && !navItems.some((item) => active === item.to || active.startsWith(item.match || item.to))) {
    return <Navigate to="/overview" replace />;
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <aside className={`app-sidebar ${menuOpen ? "app-sidebar-open" : ""}`} aria-label="控制台侧栏">
        <div className="sidebar-brand">
          <span className="brand-mark" aria-hidden="true">Q</span>
          <div><strong>量化交易</strong><span>运行控制台</span></div>
          <button className="sidebar-close" type="button" onClick={() => setMenuOpen(false)} aria-label="关闭导航"><X size={18} /></button>
        </div>
        <nav className="app-nav" aria-label="主导航">
          {navGroups.map((group) => (
            <div className="nav-section" key={group.label}>
              <span className="nav-section-label">{group.label}</span>
              <div className="nav-section-links">
                {group.items.map((item) => {
                  const Icon = item.icon;
                  const isActive = active === item.to || active.startsWith(item.match || item.to);
                  return (
                    <Link key={item.to} to={item.to} className={`nav-link ${isActive ? "nav-link-active" : ""}`} aria-current={isActive ? "page" : undefined}>
                      <Icon size={17} aria-hidden="true" /><span>{item.label}</span>
                    </Link>
                  );
                })}
              </div>
            </div>
          ))}
          <div className="nav-system-load" aria-label="服务器实时负载">
            <span className="nav-load-title">
              <Cpu size={14} aria-hidden="true" />
              服务器
            </span>
            {systemLoadQuery.data && !systemLoadQuery.isError ? (
              <div className="nav-load-meters">
                <NavLoadItem label="CPU" value={systemLoadQuery.data.cpu?.percent ?? 0} />
                <NavLoadItem label="内存" value={systemLoadQuery.data.memory?.percent ?? 0} />
                <NavLoadItem label="磁盘" value={systemLoadQuery.data.disk?.percent ?? 0} />
              </div>
            ) : (
              <span className="nav-load-empty">{systemLoadQuery.isLoading ? "读取中" : "不可用"}</span>
            )}
          </div>
        </nav>
        <div className="sidebar-session">
          <span><strong>{user || "已认证"}</strong><small>当前用户</small></span>
          <button className="icon-btn" type="button" onClick={logout} title="退出登录" aria-label="退出登录"><LogOut size={16} aria-hidden="true" />退出</button>
        </div>
      </aside>
      <button className={`sidebar-scrim ${menuOpen ? "sidebar-scrim-open" : ""}`} type="button" aria-label="关闭导航" onClick={() => setMenuOpen(false)} />
      <div className="app-content">
        <header className="mobile-topbar">
          <button className="menu-button" type="button" onClick={() => setMenuOpen(true)} aria-label="打开导航" aria-expanded={menuOpen}><Menu size={19} /></button>
          <strong>{navItems.find((item) => active === item.to || active.startsWith(item.match || item.to))?.label || "控制台"}</strong>
          <span className={`connection-dot ${systemLoadQuery.isError ? "connection-bad" : ""}`} title={systemLoadQuery.isError ? "服务器状态不可用" : "服务器在线"} />
        </header>
        <main ref={mainRef} id="main-content" className="app-main" tabIndex={-1}>{children}</main>
      </div>
    </div>
  );
}
