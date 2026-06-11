import { useAppStore } from "@/lib/store";
import MainDashboard from "@/pages/MainDashboard";
import LoginPage from "@/pages/Login";
import { Routes, Route, Navigate } from "react-router-dom";

export function App() {
  const token = useAppStore((s) => s.token);

  if (!token) return <LoginPage />;

  return (
    <Routes>
      <Route path="/*" element={<MainDashboard />} />
    </Routes>
  );
}
