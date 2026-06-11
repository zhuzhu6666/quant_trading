import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card, Input } from "@/components/ui";
import { useAppStore } from "@/lib/store";

export default function LoginPage() {
  const [pass, setPass] = useState("admin");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  async function login() {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "admin", password: pass }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        const msg =
          err?.detail?.msg ||
          err?.detail ||
          `login failed (${r.status} ${r.statusText})`;
        setError(msg);
        return;
      }
      const d = await r.json();
      useAppStore.getState().setAuth(d.token, d.user || "admin");
      navigate("/");
    } catch (e: any) {
      setError(`登录失败: ${e?.message ?? e}`);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center"
      style={{ backgroundColor: "#f5f7fa" }}
    >
      <Card className="w-80 space-y-4">
        <div
          className="text-lg font-semibold text-center"
          style={{ color: "#1a1e24" }}
        >
          Quant Console
        </div>
        <Input
          type="password"
          placeholder="password"
          value={pass}
          onChange={(e) => setPass(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && login()}
        />
        {error && (
          <div className="text-xs" style={{ color: "#f85149" }}>
            {error}
          </div>
        )}
        <Button
          onClick={login}
          disabled={loading}
          loading={loading}
          className="w-full"
        >
          {loading ? "登录中..." : "登录"}
        </Button>
      </Card>
    </div>
  );
}
