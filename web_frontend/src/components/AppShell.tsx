import { Link, Navigate, useLocation } from "react-router-dom";
import { LogOut, LayoutDashboard, BarChart3, Activity, ShieldAlert, Settings2, BrainCircuit } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";

const navItems = [
  { to: "/overview", label: "总览", icon: LayoutDashboard },
  { to: "/trading", label: "交易", icon: Activity },
  { to: "/pnl", label: "盈亏", icon: BarChart3 },
  { to: "/risk", label: "风控", icon: ShieldAlert },
  { to: "/learning", label: "学习", icon: BrainCircuit },
  { to: "/ops", label: "运维", icon: Settings2 },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const { logout, user } = useAuth();

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
      <header className="app-topbar">
        <div className="brand-wrap" aria-label="Quant Trading Console">
          <div className="brand-mark" aria-hidden="true">Q</div>
          <div>
            <div className="brand">Quant Trading Console</div>
            <div className="brand-subtitle">Live execution · risk · operations</div>
          </div>
        </div>
        <div className="topbar-right">
          <span className="session-chip">当前用户：{user || "已认证"}</span>
          <button className="icon-btn" type="button" onClick={logout} title="退出登录" aria-label="退出登录">
            <LogOut size={16} aria-hidden="true" />
            退出
          </button>
        </div>
      </header>
      <nav className="app-nav" aria-label="主导航">
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
      </nav>
      <main id="main-content" className="app-main" tabIndex={-1}>{children}</main>
    </div>
  );
}
