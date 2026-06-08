"use client";
import { useEffect, useState } from "react";
import { authFetch } from "@/lib/auth";
import { useAppStore } from "@/lib/store";
import { fmtNum, fmtPct, fmtUSD, classNames } from "@/lib/format";

export default function Overview() {
  const snapshot = useAppStore((s) => s.snapshot);
  const [jobId, setJobId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  // ── live loop control state ──
  const [liveStatus, setLiveStatus] = useState<any>(null);
  const [liveBroker, setLiveBroker] = useState<"mt5" | "ctrader">("mt5");
  const [liveBusy, setLiveBusy] = useState(false);
  const [liveMsg, setLiveMsg] = useState<string | null>(null);

  async function loadLive() {
    try {
      const r = await authFetch("/api/live/status");
      if (r.ok) setLiveStatus(await r.json());
    } catch {}
  }

  useEffect(() => {
    loadLive();
    const t = setInterval(loadLive, 5000);
    return () => clearInterval(t);
  }, []);

  async function startLive() {
    if (!window.confirm(`启动实盘 trading loop (${liveBroker})?\n需要 broker 已就绪, 详见 /api/live/status。`)) return;
    setLiveBusy(true);
    setLiveMsg(null);
    try {
      const r = await authFetch("/api/live/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ broker: liveBroker }),
      });
      const d = await r.json();
      setLiveMsg(d.ok ? `✓ live loop started (pid ${d.thread_id || d.pid})` : `✗ ${d.error}`);
      await loadLive();
    } finally {
      setLiveBusy(false);
    }
  }

  async function stopLive() {
    if (!window.confirm("停止实盘 trading loop?\n正在运行的实盘策略会立即终止, open 持仓不会被自动平仓 (用紧急平仓)。")) return;
    setLiveBusy(true);
    setLiveMsg(null);
    try {
      const r = await authFetch("/api/live/stop", { method: "POST" });
      const d = await r.json();
      setLiveMsg(d.ok ? (d.was_running ? "✓ live loop stopped" : "(no loop was running)") : `✗ ${d.error}`);
      await loadLive();
    } finally {
      setLiveBusy(false);
    }
  }

  async function emergencyClose() {
    const broker = liveStatus?.loop?.broker || liveBroker;
    if (!window.confirm(`紧急平仓 ${broker} 所有持仓?\n需后端 X-Confirm: emergency 二次校验。`)) return;
    setLiveBusy(true);
    setLiveMsg(null);
    try {
      const r = await authFetch("/api/live/emergency-close", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Confirm": "emergency" },
        body: JSON.stringify({ broker, symbol: null }),
      });
      const d = await r.json();
      setLiveMsg(d.ok ? `✓ ${d.broker} ${d.symbol} 已平` : `✗ ${d.error || "failed"}`);
      await loadLive();
    } finally {
      setLiveBusy(false);
    }
  }

  async function runBacktest() {
    setRunning(true);
    try {
      const r = await authFetch("/api/backtest/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: "XAUUSD+", timeframe: "M15" }),
      });
      const data = await r.json();
      setJobId(data.job_id);
    } finally {
      setRunning(false);
    }
  }

  // ── helpers ──
  const source = snapshot?.source ?? "none";
  const isLive = source === "live";
  const loopRunning = liveStatus?.loop?.running ?? false;
  const liveAccount = snapshot?.live?.account;
  const mt5Status = liveStatus?.mt5?.status;
  const ctraderStatus = liveStatus?.ctrader?.status;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">总览</h1>
        <div className="flex items-center gap-2 text-sm">
          <span className={classNames(
            "px-2 py-1 rounded text-xs font-semibold",
            isLive ? "bg-up/20 text-up" :
            source === "paper" ? "bg-accent/20 text-accent" :
            "bg-warn/20 text-warn"
          )}>
            {isLive ? `● LIVE (${snapshot?.broker})` : source === "paper" ? "● PAPER" : "● OFFLINE"}
          </span>
          <span className="text-fg-muted text-xs">{snapshot?.server_time ?? "--"}</span>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <Card title="账户权益">
          <div className="num text-3xl font-bold">{snapshot ? fmtNum(snapshot.equity) : "--"}</div>
          <div className="text-fg-muted text-sm">
            余额 {snapshot ? fmtNum(snapshot.balance) : "--"}
            {isLive && snapshot?.currency && <span className="ml-2 text-xs">({snapshot.currency})</span>}
          </div>
          {isLive && snapshot?.leverage && (
            <div className="text-fg-muted text-xs mt-1">杠杆 1:{snapshot.leverage}</div>
          )}
        </Card>

        <Card title="今日 PnL">
          <div className={classNames("num text-3xl font-bold", (snapshot?.pnl_today ?? 0) >= 0 ? "text-up" : "text-down")}>
            {snapshot ? fmtUSD(snapshot.pnl_today) : "--"}
          </div>
          <div className="text-fg-muted text-sm">
            交易 {snapshot?.daily.trades ?? 0} 胜 {snapshot?.daily.win ?? 0} 负 {snapshot?.daily.loss ?? 0}
          </div>
          {isLive && (
            <div className="text-fg-muted text-xs mt-1">
              持仓 {snapshot?.n_positions ?? 0} 个
            </div>
          )}
        </Card>

        <Card title="当前持仓">
          <div className={classNames("text-3xl font-bold",
            snapshot?.position?.dir === "LONG" ? "text-up" :
            snapshot?.position?.dir === "SHORT" ? "text-down" : "text-fg-muted"
          )}>
            {snapshot?.position?.dir ?? "FLAT"}
          </div>
          {snapshot?.position?.dir !== "FLAT" && snapshot && (
            <div className="text-fg-muted text-sm num">
              @ {fmtNum(snapshot.position.entry)} 浮动 {fmtUSD(snapshot.position.unrealized)}
              {snapshot.position.size > 0 && <span className="ml-2">× {snapshot.position.size} lot</span>}
            </div>
          )}
        </Card>

        <Card title="风控">
          <div className="num text-2xl">
            DD <span className="text-warn">{fmtPct(snapshot?.daily.drawdown_pct ?? 0)}</span>
          </div>
          <div className="text-fg-muted text-sm">
            连续亏损 {snapshot?.risk.consecutive_loss ?? 0}  熔断 {snapshot?.risk.circuit_breaker ? "触发" : "正常"}
          </div>
        </Card>

        <Card title="回测">
          <button
            onClick={runBacktest}
            disabled={running}
            className="bg-accent text-bg font-semibold px-4 py-2 rounded disabled:opacity-50"
          >
            {running ? "提交中..." : "▶ 跑一次回测"}
          </button>
          {jobId && <div className="text-fg-muted text-sm mt-2">job: {jobId}</div>}
        </Card>

        <Card title={isLive ? "实盘信息" : "实盘 / 模拟盘"}>
          {isLive ? (
            <div className="space-y-1 text-sm">
              <div className="text-fg-muted">Broker: <span className="text-fg font-semibold">{snapshot?.broker}</span></div>
              {liveAccount && (
                <>
                  <div className="text-fg-muted">Account: <span className="text-fg">{liveAccount.login || "--"}</span></div>
                  <div className="text-fg-muted">Margin: <span className="num text-fg">{fmtNum(snapshot?.margin ?? 0)}</span></div>
                  <div className="text-fg-muted">Free: <span className="num text-fg">{fmtNum(snapshot?.margin_free ?? 0)}</span></div>
                </>
              )}
            </div>
          ) : (
            <div className="text-fg-muted text-sm space-y-1">
              <div>当前显示: <span className="text-fg">模拟盘 (paper)</span></div>
              <div>启动实盘 trading loop 后, 卡片自动切换到 broker 真实数据。</div>
            </div>
          )}
        </Card>
      </div>

      {/* ── Live trading control panel ── */}
      <div className="bg-bg-card border border-bg-border rounded-lg p-4 space-y-3">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold text-fg-muted">实盘 trading loop</div>
          <div className="flex items-center gap-2 text-xs">
            <span className={classNames("w-2 h-2 rounded-full", loopRunning ? "bg-up animate-pulse" : "bg-fg-muted")} />
            <span className="text-fg-muted">
              {loopRunning ? `运行中 (${liveStatus?.loop?.broker})` : "未启动"}
            </span>
          </div>
        </div>

        {/* broker status badges */}
        <div className="flex gap-3 text-xs">
          <span className={classNames(
            "px-2 py-0.5 rounded",
            mt5Status === "connected" ? "bg-up/20 text-up" :
            mt5Status === "error" ? "bg-down/20 text-down" :
            "bg-fg-muted/20 text-fg-muted"
          )}>
            MT5: {mt5Status ?? "..."}
          </span>
          <span className={classNames(
            "px-2 py-0.5 rounded",
            ctraderStatus === "token_present" || ctraderStatus === "connected" ? "bg-up/20 text-up" :
            ctraderStatus === "no_token" ? "bg-warn/20 text-warn" :
            "bg-fg-muted/20 text-fg-muted"
          )}>
            cTrader: {ctraderStatus ?? "..."}
          </span>
        </div>

        {/* control row */}
        <div className="flex flex-wrap gap-2 items-center">
          <select
            value={liveBroker}
            onChange={(e) => setLiveBroker(e.target.value as "mt5" | "ctrader")}
            disabled={loopRunning || liveBusy}
            className="bg-bg border border-bg-border rounded px-2 py-1 text-sm disabled:opacity-50"
          >
            <option value="mt5">MT5</option>
            <option value="ctrader">cTrader</option>
          </select>
          {!loopRunning ? (
            <button
              onClick={startLive}
              disabled={liveBusy}
              className="bg-up text-bg font-semibold px-4 py-1.5 rounded text-sm disabled:opacity-50"
            >
              ▶ 启动实盘
            </button>
          ) : (
            <button
              onClick={stopLive}
              disabled={liveBusy}
              className="bg-warn text-bg font-semibold px-4 py-1.5 rounded text-sm disabled:opacity-50"
            >
              ⏹ 停止实盘
            </button>
          )}
          <button
            onClick={emergencyClose}
            disabled={liveBusy}
            className="bg-down text-bg font-semibold px-4 py-1.5 rounded text-sm disabled:opacity-50"
          >
            ⏮ 紧急平仓
          </button>
          <a
            href="/live"
            className="text-xs text-fg-muted hover:text-fg ml-auto"
          >
            详情 →
          </a>
        </div>

        {liveMsg && (
          <div className={classNames("text-xs", liveMsg.startsWith("✓") ? "text-up" : "text-down")}>
            {liveMsg}
          </div>
        )}
        <div className="text-xs text-fg-muted">
          v1 minimal loop: 60s tick, 读 MT5 真实账户 + 持仓, 写到 logs/live_loop.log。
          策略集成 (multi_factor_m15 等) 是 Phase 4+。
        </div>
      </div>
    </div>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-bg-card border border-bg-border rounded-lg p-4">
      <div className="text-fg-muted text-sm mb-2">{title}</div>
      {children}
    </div>
  );
}
