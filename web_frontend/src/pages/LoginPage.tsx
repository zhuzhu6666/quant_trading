import { FormEvent, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { authenticated, loading, login } = useAuth();
  const [username, setUsername] = useState("zhu");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [working, setWorking] = useState(false);

  useEffect(() => {
    if (!loading && authenticated) {
      const from = typeof (location.state as { from?: string })?.from === "string"
        ? (location.state as { from?: string }).from
        : "/overview";
      navigate(from || "/overview", { replace: true });
    }
  }, [authenticated, loading, location.state, navigate]);

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    setWorking(true);
    try {
      await login({ username: username.trim(), password });
      navigate("/overview", { replace: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : "登录失败");
    } finally {
      setWorking(false);
    }
  };

  return (
    <div className="auth-root">
      <form className="panel auth-card" onSubmit={onSubmit} aria-label="登录 Quant Trading Console">
        <div className="auth-card-head">
          <div className="brand-mark auth-brand-mark" aria-hidden="true">Q</div>
          <div>
            <div className="eyebrow">Web Console</div>
            <h1>登录</h1>
          </div>
        </div>
        <label className="form-row">
          <span>用户名</span>
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
        </label>
        <label className="form-row">
          <span>密码</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        <button className="action-btn action-primary" type="submit" disabled={working}>
          {working ? "登录中..." : "进入操作台"}
        </button>
        {error ? <p className="error-text" role="alert">{error}</p> : null}
      </form>
      <p className="muted">支持账号：zhu</p>
    </div>
  );
}
