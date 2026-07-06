import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/AppShell";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { LoginPage } from "@/pages/LoginPage";
import { OverviewPage } from "@/pages/OverviewPage";
import { TradingPage } from "@/pages/TradingPage";
import { PnlPage } from "@/pages/PnlPage";
import { RiskPage } from "@/pages/RiskPage";
import { OpsPage } from "@/pages/OpsPage";
import { LearningPage } from "@/pages/LearningPage";
import { ModelsPage } from "@/pages/ModelsPage";
import { V15CockpitPage } from "@/pages/V15CockpitPage";
import { V16BrainPage } from "@/pages/V16BrainPage";
import { useState } from "react";

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
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/"
          element={<Navigate to="/overview" replace />}
        />
        <Route
          path="/overview"
          element={
            <ProtectedRoute>
              <ErrorBoundary>
                <AppShell>
                  <OverviewPage />
                </AppShell>
              </ErrorBoundary>
            </ProtectedRoute>
          }
        />
        <Route
          path="/trading"
          element={
            <ProtectedRoute>
              <ErrorBoundary>
                <AppShell>
                  <TradingPage />
                </AppShell>
              </ErrorBoundary>
            </ProtectedRoute>
          }
        />
        <Route
          path="/pnl"
          element={
            <ProtectedRoute>
              <ErrorBoundary>
                <AppShell>
                  <PnlPage />
                </AppShell>
              </ErrorBoundary>
            </ProtectedRoute>
          }
        />
        <Route
          path="/risk"
          element={
            <ProtectedRoute>
              <ErrorBoundary>
                <AppShell>
                  <RiskPage />
                </AppShell>
              </ErrorBoundary>
            </ProtectedRoute>
          }
        />
        <Route
          path="/learning"
          element={
            <ProtectedRoute>
              <ErrorBoundary>
                <AppShell>
                  <LearningPage />
                </AppShell>
              </ErrorBoundary>
            </ProtectedRoute>
          }
        />
        <Route
          path="/models"
          element={
            <ProtectedRoute>
              <ErrorBoundary>
                <AppShell>
                  <ModelsPage />
                </AppShell>
              </ErrorBoundary>
            </ProtectedRoute>
          }
        />
        <Route
          path="/v15"
          element={
            <ProtectedRoute>
              <ErrorBoundary>
                <AppShell>
                  <V15CockpitPage />
                </AppShell>
              </ErrorBoundary>
            </ProtectedRoute>
          }
        />
        <Route
          path="/v16"
          element={
            <ProtectedRoute>
              <ErrorBoundary>
                <AppShell>
                  <V16BrainPage />
                </AppShell>
              </ErrorBoundary>
            </ProtectedRoute>
          }
        />
        <Route
          path="/ops"
          element={
            <ProtectedRoute>
              <ErrorBoundary>
                <AppShell>
                  <OpsPage />
                </AppShell>
              </ErrorBoundary>
            </ProtectedRoute>
          }
        />
        <Route path="*" element={<Navigate to="/overview" replace />} />
      </Routes>
    </QueryClientProvider>
  );
}
