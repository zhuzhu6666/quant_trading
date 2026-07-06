import { Link, Navigate, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { LogOut, LayoutDashboard, BarChart3, Activity, ShieldAlert, Settings2, BrainCircuit, Microscope, Cpu, Rocket } from "lucide-react";
import { getSystemLoad } from "@/api/client";
import { useAuth } from "@/contexts/AuthContext";
import { formatDecimal } from "@/lib/format";

const navItems = [
  { to: "/overview", label: "总览", icon: LayoutDashboard },
  { to: "/trading", label: "交易", icon: Activity },
  { to: "/pnl", label: "盈亏", icon: BarChart3 },
  { to: "/risk", label: "风控", icon: ShieldAlert },
  { to: "/learning", label: "学习", icon: BrainCircuit },
  { to: "/models", label: "模型", icon: Microscope },
  { to: "/v15", label: "V15", icon: Rocket },
  { to: "/v16", label: "V16", icon: BrainCircuit },
  { to: "/ops", label: "运维", icon: Settings2 },
];

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
  const { logout, user } = useAuth();
  const systemLoadQuery = useQuery({
    queryKey: ["system-load", "chrome"],
    queryFn: getSystemLoad,
    refetchInterval: 5_000,
    staleTime: 2_500,
    enabled: location.pathname !== "/login",
  });

  const active = location.pathname;
  if (active === "/login") {
    return <>{children}</>;
  }

  if (!navItems.some((item) => item.to === active)) {
    return <Navigate to="/overview" replace />;
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <div className="app-chrome">
        <nav className="app-nav" aria-label="主导航">
          <div className="nav-link-group">
            {navItems.map((item) => {
              const Icon = item.icon;
              const isActive = active === item.to;
              return (
                <Link
                  key={item.to}
                  to={item.to}
                  className={`nav-link ${isActive ? "nav-link-active" : ""}`}
                  aria-current={isActive ? "page" : undefined}
                >
                  <Icon size={16} aria-hidden="true" />
                  <span>{item.label}</span>
                </Link>
              );
            })}
          </div>
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
          <div className="topbar-right">
            <span className="session-chip">当前用户：{user || "已认证"}</span>
            <button className="icon-btn" type="button" onClick={logout} title="退出登录" aria-label="退出登录">
              <LogOut size={16} aria-hidden="true" />
              退出
            </button>
          </div>
        </nav>
      </div>
      <main id="main-content" className="app-main" tabIndex={-1}>{children}</main>
    </div>
  );
}
