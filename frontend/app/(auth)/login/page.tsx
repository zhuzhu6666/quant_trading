"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("zhu");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    setBusy(true);
    setErr(null);
    try {
      const r = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      const d = await r.json();
      if (d.token) {
        localStorage.setItem("quant_user", d.user);
        localStorage.setItem("quant_token", d.token);
        router.push("/");
      } else {
        setErr("登录失败");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-bg">
      <div className="bg-bg-card border border-bg-border rounded-lg p-8 w-96 space-y-4">
        <div className="text-2xl font-bold text-accent">⚡ Quant Console</div>
        <div className="text-fg-muted text-sm">登录 (v1 stub: 任意密码即可)</div>
        <div>
          <label className="text-fg-muted text-xs">用户名</label>
          <input value={username} onChange={(e) => setUsername(e.target.value)} className="w-full bg-bg border border-bg-border rounded px-3 py-2" />
        </div>
        <div>
          <label className="text-fg-muted text-xs">密码</label>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} className="w-full bg-bg border border-bg-border rounded px-3 py-2" />
        </div>
        {err && <div className="text-down text-sm">{err}</div>}
        <button onClick={submit} disabled={busy} className="w-full bg-accent text-bg font-semibold py-2 rounded disabled:opacity-50">
          {busy ? "登录中..." : "登录"}
        </button>
        <div className="text-xs text-fg-muted text-center">
          第一次访问? 默认用户 <code className="text-fg">zhu</code>。v2 接入 JWT + OAuth。
        </div>
      </div>
    </div>
  );
}
