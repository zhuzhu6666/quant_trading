import { FormEvent, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Eye, EyeOff } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { authenticated, loading, login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
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
      <form className="panel auth-card" onSubmit={onSubmit} aria-label="登录量化交易控制台">
        <div className="auth-card-head">
          <div className="brand-mark auth-brand-mark" aria-hidden="true">Q</div>
          <div>
            <div className="eyebrow">量化交易控制台</div>
            <h1>登录系统</h1>
          </div>
        </div>
        <label className="form-row">
          <span>用户名</span>
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
        </label>
        <label className="form-row">
          <span>密码</span>
          <span className="password-field">
            <input
              type={showPassword ? "text" : "password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
            <button type="button" onClick={() => setShowPassword((value) => !value)} aria-label={showPassword ? "隐藏密码" : "显示密码"}>
              {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
            </button>
          </span>
        </label>
        <button className="action-btn action-primary" type="submit" disabled={working}>
          {working ? "登录中..." : "进入操作台"}
        </button>
        {error ? <p className="error-text" role="alert">{error}</p> : null}
      </form>
    </div>
  );
}
