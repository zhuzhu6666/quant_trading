import { Navigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";

export function ProtectedRoute({ children }: { children: JSX.Element }) {
  const { loading, authenticated } = useAuth();

  if (loading) {
    return <div className="loading-state">正在校验登录态...</div>;
  }

  if (!authenticated) {
    return <Navigate to="/login" replace />;
  }

  return children;
}
