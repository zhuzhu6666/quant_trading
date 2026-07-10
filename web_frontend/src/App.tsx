import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense, useState } from "react";
import { Navigate, Outlet, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/AppShell";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { ProtectedRoute } from "@/components/ProtectedRoute";
const LoginPage = lazy(() => import("@/pages/LoginPage").then((module) => ({ default: module.LoginPage })));
const OverviewPage = lazy(() => import("@/pages/OverviewPage").then((module) => ({ default: module.OverviewPage })));
const TradingPage = lazy(() => import("@/pages/TradingPage").then((module) => ({ default: module.TradingPage })));
const PnlPage = lazy(() => import("@/pages/PnlPage").then((module) => ({ default: module.PnlPage })));
const RiskPage = lazy(() => import("@/pages/RiskPage").then((module) => ({ default: module.RiskPage })));
const OpsPage = lazy(() => import("@/pages/OpsPage").then((module) => ({ default: module.OpsPage })));
const LearningPage = lazy(() => import("@/pages/LearningPage").then((module) => ({ default: module.LearningPage })));
const ModelsPage = lazy(() => import("@/pages/ModelsPage").then((module) => ({ default: module.ModelsPage })));
const V15CockpitPage = lazy(() => import("@/pages/V15CockpitPage").then((module) => ({ default: module.V15CockpitPage })));
const V16BrainPage = lazy(() => import("@/pages/V16BrainPage").then((module) => ({ default: module.V16BrainPage })));

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
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            retry: 1,
            staleTime: 2000,
            refetchOnWindowFocus: false,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <Routes>
        <Route path="/login" element={<Suspense fallback={<RouteFallback />}><LoginPage /></Suspense>} />
        <Route
          path="/"
          element={<Navigate to="/overview" replace />}
        />
        <Route element={<ProtectedAppLayout />}>
          <Route path="/overview" element={<OverviewPage />} />
          <Route path="/trading" element={<TradingPage />} />
          <Route path="/pnl" element={<PnlPage />} />
          <Route path="/risk" element={<RiskPage />} />
          <Route path="/learning" element={<LearningPage />} />
          <Route path="/models" element={<ModelsPage />} />
          <Route path="/v15" element={<V15CockpitPage />} />
          <Route path="/v16" element={<V16BrainPage />} />
          <Route path="/ops" element={<OpsPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/overview" replace />} />
      </Routes>
    </QueryClientProvider>
  );
}
