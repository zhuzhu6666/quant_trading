import { lazy, Suspense } from "react";
import { Navigate, Outlet, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/AppShell";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { ProtectedRoute } from "@/components/ProtectedRoute";
const LoginPage = lazy(() => import("@/pages/LoginPage").then((module) => ({ default: module.LoginPage })));
const OverviewPage = lazy(() => import("@/pages/OverviewPage").then((module) => ({ default: module.OverviewPage })));
const TradingPage = lazy(() => import("@/pages/TradingPage").then((module) => ({ default: module.TradingPage })));
const OpsPage = lazy(() => import("@/pages/OpsPage").then((module) => ({ default: module.OpsPage })));
const PerformanceWorkspace = lazy(() => import("@/pages/WorkspacePages").then((module) => ({ default: module.PerformanceWorkspace })));
const GovernanceWorkspace = lazy(() => import("@/pages/WorkspacePages").then((module) => ({ default: module.GovernanceWorkspace })));
const AutonomyWorkspace = lazy(() => import("@/pages/WorkspacePages").then((module) => ({ default: module.AutonomyWorkspace })));

function RouteFallback() {
  return <div className="route-loading" role="status" aria-live="polite"><span />正在加载控制台…</div>;
}

function ProtectedAppLayout() {
  return (
    <ProtectedRoute>
      <ErrorBoundary>
        <AppShell><Suspense fallback={<RouteFallback />}><Outlet /></Suspense></AppShell>
      </ErrorBoundary>
    </ProtectedRoute>
  );
}

export function App() {
  return (
      <Routes>
        <Route path="/login" element={<Suspense fallback={<RouteFallback />}><LoginPage /></Suspense>} />
        <Route
          path="/"
          element={<Navigate to="/overview" replace />}
        />
        <Route element={<ProtectedAppLayout />}>
          <Route path="/overview" element={<OverviewPage />} />
          <Route path="/trading" element={<TradingPage />} />
          <Route path="/performance/:section" element={<PerformanceWorkspace />} />
          <Route path="/governance/:section" element={<GovernanceWorkspace />} />
          <Route path="/autonomy/:section" element={<AutonomyWorkspace />} />
          <Route path="/pnl" element={<Navigate to="/performance/pnl" replace />} />
          <Route path="/risk" element={<Navigate to="/performance/risk" replace />} />
          <Route path="/learning" element={<Navigate to="/governance/learning" replace />} />
          <Route path="/models" element={<Navigate to="/governance/models" replace />} />
          <Route path="/v15" element={<Navigate to="/autonomy/runtime" replace />} />
          <Route path="/v16" element={<Navigate to="/autonomy/chain" replace />} />
          <Route path="/ops" element={<OpsPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/overview" replace />} />
      </Routes>
  );
}
