import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
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
          `登录失败 (${r.status} ${r.statusText})`;
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
    <div className="min-h-screen flex items-center justify-center bg-apple-bg">
      <Card className="w-80 space-y-5" padding="lg">
        <div className="text-center">
          <div className="text-2xl font-semibold text-text-primary tracking-tight mb-1">
            ◆ Quant
          </div>
          <div className="text-xs text-text-secondary">
            XAUUSD+ 量化交易控制台
          </div>
        </div>
        <Input
          type="password"
          placeholder="请输入密码"
          value={pass}
          onChange={(e) => setPass(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && login()}
        />
        {error && (
          <div className="text-xs text-danger bg-danger-light rounded-lg px-3 py-2">
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
