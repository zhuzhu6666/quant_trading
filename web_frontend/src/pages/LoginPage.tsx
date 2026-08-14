import { FormEvent, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ArrowRight, Eye, EyeOff, ShieldCheck } from "lucide-react";
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
        : "/trade-ops";
      navigate(from || "/trade-ops", { replace: true });
    }
  }, [authenticated, loading, location.state, navigate]);

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    setWorking(true);
    try {
      await login({ username: username.trim(), password });
      navigate("/trade-ops", { replace: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : "登录失败");
    } finally {
      setWorking(false);
    }
  };

  return (
    <div className="auth-root">
      <main className="auth-shell">
        <section className="auth-hero" aria-labelledby="auth-hero-title">
          <div className="auth-brand-lockup"><div className="auth-brand-mark" aria-hidden="true">Q</div><div><strong>QUANT</strong><span>WORKBENCH</span></div></div>
          <div className="auth-hero-copy">
            <span className="auth-kicker">SERVER-AUTHORITATIVE DESK</span>
            <h1 id="auth-hero-title">量化交易<br /><em>操作台</em></h1>
            <p>连接服务端事实，查看市场、风险、学习与执行链路。所有高影响动作都由后端复核。</p>
          </div>
          <div className="auth-hero-facts" aria-label="客户端能力">
            <div><span>运行方式</span><strong>Tauri 2 桌面端</strong></div>
            <div><span>事实来源</span><strong>API / WSS</strong></div>
            <div><span>安全边界</span><strong>服务端门控</strong></div>
          </div>
        </section>
        <form className="auth-card" onSubmit={onSubmit} aria-label="登录量化交易控制台">
          <div className="auth-card-head">
            <div className="auth-card-overline"><span className="auth-status-dot" aria-hidden="true" />SECURE ACCESS</div>
            <h2>登录操作台</h2>
            <p>验证服务端账户后进入 Workbench。</p>
          </div>
          <div className="auth-form-fields">
            <label className="auth-field" htmlFor="auth-username"><span>用户名</span><input id="auth-username" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" placeholder="输入用户名" required /></label>
            <label className="auth-field" htmlFor="auth-password"><span>密码</span><span className="auth-input-wrap"><input id="auth-password" type={showPassword ? "text" : "password"} value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" placeholder="输入密码" required /><button className="auth-input-action" type="button" onClick={() => setShowPassword((value) => !value)} aria-label={showPassword ? "隐藏密码" : "显示密码"}>{showPassword ? <EyeOff size={17} /> : <Eye size={17} />}</button></span></label>
          </div>
          <button className="auth-submit" type="submit" disabled={working} aria-busy={working}><span>{working ? "正在验证..." : "进入操作台"}</span>{working ? <span className="auth-submit-pulse" aria-hidden="true" /> : <ArrowRight size={17} />}</button>
          {error ? <p className="auth-error" role="alert">{error}</p> : null}
          <p className="auth-card-note"><ShieldCheck size={15} />登录凭据只用于本次会话验证，不写入页面状态。</p>
        </form>
      </main>
    </div>
  );
}
