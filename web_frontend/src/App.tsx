import { lazy, Suspense } from "react";
import { Navigate, Outlet, Route, Routes } from "react-router-dom";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { useAuth } from "@/contexts/AuthContext";
import { LiveStateProvider } from "@/hooks/useLiveState";
import { WorkbenchShell } from "@/shell/WorkbenchShell";

const LoginPage = lazy(() => import("@/pages/LoginPage").then((module) => ({ default: module.LoginPage })));
const TradeOpsPage = lazy(() => import("@/pages/TradeOpsPage").then((module) => ({ default: module.TradeOpsPage })));
const RiskDeskPage = lazy(() => import("@/pages/RiskDeskPage").then((module) => ({ default: module.RiskDeskPage })));
const ResearchPage = lazy(() => import("@/pages/ResearchPage").then((module) => ({ default: module.ResearchPage })));
const GovernancePage = lazy(() => import("@/pages/GovernancePage").then((module) => ({ default: module.GovernancePage })));
const OpsPage = lazy(() => import("@/pages/OpsPage").then((module) => ({ default: module.OpsPage })));

function RouteFallback() {
  return <div className="route-loading" role="status" aria-live="polite"><span />加载工作区…</div>;
}

function RouteDeprecatedPage() {
  return <div className="route-deprecated" role="alert"><span className="route-deprecated-code">404 / ROUTE_DEPRECATED</span><h1>此地址已废弃</h1><p>旧版页面和 section alias 不再自动跳转。请从左侧工作区导航进入当前操作台。</p></div>;
}

function ProtectedAppLayout() {
  const { authenticated } = useAuth();
  return <ProtectedRoute><LiveStateProvider enabled={authenticated}><ErrorBoundary><WorkbenchShell /></ErrorBoundary></LiveStateProvider></ProtectedRoute>;
}

export function App() {
  return <Routes>
    <Route path="/login" element={<Suspense fallback={<RouteFallback />}><LoginPage /></Suspense>} />
    <Route path="/" element={<Navigate to="/trade-ops" replace />} />
    <Route element={<ProtectedAppLayout />}>
      <Route path="/trade-ops" element={<Suspense fallback={<RouteFallback />}><TradeOpsPage /></Suspense>} />
      <Route path="/risk-desk" element={<Suspense fallback={<RouteFallback />}><RiskDeskPage /></Suspense>} />
      <Route path="/research" element={<Suspense fallback={<RouteFallback />}><ResearchPage /></Suspense>} />
      <Route path="/governance" element={<Suspense fallback={<RouteFallback />}><GovernancePage /></Suspense>} />
      <Route path="/ops" element={<Suspense fallback={<RouteFallback />}><OpsPage /></Suspense>} />
      <Route path="*" element={<RouteDeprecatedPage />} />
    </Route>
  </Routes>;
}
